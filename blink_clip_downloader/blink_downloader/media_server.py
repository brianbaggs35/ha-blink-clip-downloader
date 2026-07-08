"""HTTP media server: REST API + embedded SPA with Video.js media player."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import platform
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from aiohttp import web

from .database import ClipDatabase

if TYPE_CHECKING:
    from .analysis_queue import AnalysisQueue
    from .analyzer import BaseAnalyzer, MoondreamFineTuneManager
    from .notification_channels import NotificationDispatcher

_LOGGER = logging.getLogger(__name__)

_CLIP_NOT_FOUND = "Clip not found"

# ---------------------------------------------------------------------------
# Moondream local install state (persists for the lifetime of the process)
# ---------------------------------------------------------------------------

_MOONDREAM_PACKAGES_DIR = Path("/data/moondream_packages")

# Pinned to the >=1.3,<2 range for the same reason as the Dockerfile's build-time
# install — see the comment there and analyzer.py's
# MoondreamLocalAnalyzer._load_model_sync for the version-drift incident this
# guards against.
_MOONDREAM_PIP_SPEC = "moondream>=1.3,<2"

_moondream_install_state: dict = {"status": "idle", "log": ""}


def _moondream_arch_supported() -> bool:
    """Return True only on x86_64 — moondream has no musllinux aarch64 wheels."""
    return platform.machine() == "x86_64"


def _is_moondream_installed() -> bool:
    pkg = str(_MOONDREAM_PACKAGES_DIR)
    if _MOONDREAM_PACKAGES_DIR.exists() and pkg not in sys.path:
        sys.path.insert(0, pkg)
    try:
        import moondream  # noqa: PLC0415, F401  # type: ignore[import-not-found]

        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

# Content-Security-Policy that allows Video.js (jsDelivr CDN) while
# restricting everything else to same-origin.
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' cdn.jsdelivr.net data:; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob:; "
    "font-src 'self' cdn.jsdelivr.net data:; "
    "connect-src 'self'"
)


@web.middleware
async def _security_middleware(
    request: web.Request, handler: Callable
) -> web.StreamResponse:
    """Attach security headers to every non-streaming response."""
    response = await handler(request)
    if not response.prepared:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        if response.content_type == "text/html":
            response.headers.setdefault("Content-Security-Policy", _CSP)
    return response


# ---------------------------------------------------------------------------
# Embedded SPA HTML – Library | Status | Automations  +  Video.js player
# ---------------------------------------------------------------------------

_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Blink Clip Library</title>
<!-- Video.js 8.x – open-source HTML5 video player (MIT) -->
<link href="https://cdn.jsdelivr.net/npm/video.js@8.10.0/dist/video-js.min.css" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/video.js@8.10.0/dist/video.min.js"></script>
<style>
/* Dark theme — the default as of v4.0.0 (this add-on ships dark-first).
   :root itself now holds the dark palette; body.light is the explicit
   opt-out below. There is no prefers-color-scheme fallback anymore — see
   the theme-init script for why (a deliberate product default, not
   OS-driven). */
:root{
  --bg:#0d1117;--surface:#161b22;--card:#1c2128;--card2:#21262d;
  --border:#30363d;--accent:#58a6ff;--accent2:#1f6feb;--success:#3fb950;
  --danger:#f85149;--warn:#d29922;--text:#c9d1d9;--muted:#8b949e;
  --starred:#e3b341;--radius:10px;--nav-h:56px;--nav-w:225px;
  --btn-text:#0d1117;--badge-ok-bg:#0d2818;--badge-err-bg:#2d1313;
  --tag-bg:#1a3055;--tag-text:#79b8ff;--code-color:#a9d1f7
}
/* Manual override classes (toggled by the ☀/🌙 button, stored in localStorage) */
body.dark{
  --bg:#0d1117;--surface:#161b22;--card:#1c2128;--card2:#21262d;
  --border:#30363d;--accent:#58a6ff;--accent2:#1f6feb;--success:#3fb950;
  --danger:#f85149;--warn:#d29922;--text:#c9d1d9;--muted:#8b949e;
  --starred:#e3b341;
  --btn-text:#0d1117;--badge-ok-bg:#0d2818;--badge-err-bg:#2d1313;
  --tag-bg:#1a3055;--tag-text:#79b8ff;--code-color:#a9d1f7
}
body.light{
  --bg:#f3f4f6;--surface:#ffffff;--card:#f9fafb;--card2:#f0f2f5;
  --border:#e5e7eb;--accent:#2563eb;--accent2:#1d4ed8;--success:#16a34a;
  --danger:#dc2626;--warn:#d97706;--text:#111827;--muted:#6b7280;
  --starred:#f59e0b;
  --btn-text:#ffffff;--badge-ok-bg:#dcfce7;--badge-err-bg:#fee2e2;
  --tag-bg:#dbeafe;--tag-text:#1d4ed8;--code-color:#1e40af
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
     background:var(--bg);color:var(--text);
     /* dvh re-measures on iOS Companion WKWebView bg/fg cycles; vh is the fallback */
     height:100vh;height:100dvh;display:flex;
     /* Sidebar dashboard shell on desktop — nav is a column on the left,
        the active .page fills the rest. Reverts to a top bar on narrow
        viewports, see @media(max-width:600px) at the end of this stylesheet. */
     flex-direction:row;overflow:hidden}
button,input,select{font:inherit}
a{color:var(--accent);text-decoration:none}
code{background:var(--card2);border:1px solid var(--border);border-radius:4px;
     padding:.1em .4em;font-family:monospace;font-size:.85em}

/* ── Navigation (sidebar) ──────────────────────────────── */
.nav{background:var(--surface);border-right:1px solid var(--border);
     width:var(--nav-w);display:flex;flex-direction:column;align-items:stretch;
     gap:.3rem;padding:1.1rem .85rem;flex-shrink:0;z-index:10;overflow-y:auto}
.nav-brand{font-size:1.18rem;font-weight:700;color:var(--accent);
           white-space:nowrap;margin-bottom:.9rem;padding:0 .3rem}
.nav-brand span{opacity:.5;font-weight:400}
.nav-tabs{display:flex;flex-direction:column;gap:.15rem;flex:1}
.nav-tab{background:transparent;border:none;color:var(--muted);
         width:100%;text-align:left;display:flex;align-items:center;gap:.6rem;
         padding:.55rem .7rem;border-radius:var(--radius);cursor:pointer;
         font-size:.88rem;font-weight:500;transition:.15s;white-space:nowrap}
.nav-tab:hover{color:var(--text);background:var(--card)}
.nav-tab.active{color:var(--accent);background:var(--card2)}
.nav-actions{display:flex;align-items:center;gap:.4rem;flex-wrap:wrap;
             margin-top:.6rem;padding-top:.6rem;border-top:1px solid var(--border)}

.btn{background:var(--accent);color:var(--btn-text);border:none;border-radius:var(--radius);
     padding:.4rem .9rem;font-size:.85rem;font-weight:600;cursor:pointer;
     transition:.15s;white-space:nowrap;display:inline-flex;align-items:center;gap:.3rem}
.btn:hover:not(:disabled){filter:brightness(1.12)}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn.sm{padding:.3rem .65rem;font-size:.78rem}
.btn.outline{background:transparent;color:var(--accent);border:1px solid var(--accent)}
.btn.ghost{background:transparent;color:var(--muted);border:1px solid var(--border)}
.btn.ghost:hover{color:var(--text);border-color:var(--muted)}
.btn.danger{background:var(--danger);color:#fff}
.btn.icon{padding:.38rem .5rem;font-size:1rem;border-radius:6px}

.badge{display:inline-block;background:var(--border);border-radius:9px;
       padding:.1rem .5rem;font-size:.72rem;color:var(--muted)}
.badge.ok{background:var(--badge-ok-bg);color:var(--success)}
.badge.err{background:var(--badge-err-bg);color:var(--danger)}

/* ── Pages ────────────────────────────────────────────── */
.page{flex:1;overflow:hidden;display:none}
.page.active{display:flex}

/* ── Library layout ───────────────────────────────────── */
#page-library{flex-direction:column}
.lib-filters{background:var(--surface);border-bottom:1px solid var(--border);
             padding:.55rem 1rem;display:flex;align-items:center;gap:.5rem;
             flex-wrap:wrap;flex-shrink:0}
.search{background:var(--card);border:1px solid var(--border);
        border-radius:var(--radius);padding:.38rem .75rem;color:var(--text);
        font-size:.88rem;width:190px}
.search:focus{outline:none;border-color:var(--accent)}
.sel{background:var(--card);border:1px solid var(--border);
     border-radius:var(--radius);padding:.38rem .6rem;color:var(--text);
     font-size:.82rem;cursor:pointer}
.sel:focus{outline:none;border-color:var(--accent)}
.chk{display:flex;align-items:center;gap:.3rem;font-size:.82rem;
     white-space:nowrap;cursor:pointer;user-select:none;color:var(--muted)}
.chk input{accent-color:var(--accent)}
.lib-body{display:flex;flex:1;overflow:hidden}
.sidebar{width:196px;background:var(--surface);border-right:1px solid var(--border);
         overflow-y:auto;flex-shrink:0;padding:.75rem 0}
.sb-head{padding:.3rem 1rem .1rem;font-size:.68rem;font-weight:700;
         text-transform:uppercase;letter-spacing:.1em;color:var(--muted)}
.cam-item{display:flex;justify-content:space-between;align-items:center;
          padding:.4rem 1rem;cursor:pointer;font-size:.84rem;
          border-left:3px solid transparent;transition:.1s}
.cam-item:hover{background:var(--card)}
.cam-item.active{border-left-color:var(--accent);color:var(--accent);background:var(--card2)}
.cam-badge{background:var(--border);border-radius:8px;
           padding:.05rem .4rem;font-size:.72rem;color:var(--muted)}
.lib-main{flex:1;overflow-y:auto;padding:1rem}

/* ── Stats bar ────────────────────────────────────────── */
.stats-row{display:flex;gap:.65rem;margin-bottom:.85rem;flex-wrap:wrap;align-items:center}
.stat-chip{background:var(--card);border:1px solid var(--border);
           border-radius:var(--radius);padding:.32rem .7rem;font-size:.8rem;white-space:nowrap}
.stat-chip strong{color:var(--accent)}

/* ── Clip grid ────────────────────────────────────────── */
.clip-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(228px,100%),1fr));gap:.8rem}
.clip-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
           overflow:hidden;cursor:pointer;transition:.15s;position:relative;user-select:none;
           box-shadow:0 1px 3px rgba(0,0,0,.18)}
.clip-card:hover{border-color:var(--accent2);transform:translateY(-2px);
                 box-shadow:0 6px 18px rgba(31,111,235,.25)}
.clip-card.selected{border-color:var(--accent);box-shadow:0 0 0 2px rgba(88,166,255,.3)}
.thumb-wrap{position:relative;aspect-ratio:16/9;background:#000;overflow:hidden}
.thumb-wrap img{width:100%;height:100%;object-fit:cover;opacity:.85;transition:.2s}
.clip-card:hover .thumb-wrap img{opacity:1}
.no-thumb{display:flex;align-items:center;justify-content:center;
          height:100%;font-size:2.5rem;opacity:.18;color:#fff}
.dur-badge{position:absolute;bottom:.3rem;right:.3rem;background:rgba(0,0,0,.8);
           color:#fff;font-size:.68rem;padding:.1rem .35rem;border-radius:3px}
.star-badge{position:absolute;top:.3rem;left:.3rem;font-size:.9rem;
            color:var(--starred);filter:drop-shadow(0 1px 2px #000)}
.notified-badge{position:absolute;bottom:.3rem;left:.3rem;font-size:.9rem;
                filter:drop-shadow(0 1px 2px #000)}
.sel-check{position:absolute;top:.3rem;right:.3rem;width:17px;height:17px;
           background:rgba(0,0,0,.55);border:1.5px solid rgba(255,255,255,.35);
           border-radius:4px;display:flex;align-items:center;justify-content:center;
           font-size:.65rem;color:#fff;transition:.1s}
.clip-card.selected .sel-check{background:var(--accent);border-color:var(--accent)}
.clip-info{padding:.5rem .6rem}
.clip-camera{font-size:.77rem;font-weight:600;color:var(--accent);
             overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.clip-time{font-size:.71rem;color:var(--muted);margin:.12rem 0}
.clip-meta{font-size:.69rem;color:var(--muted);display:flex;gap:.35rem;flex-wrap:wrap}
.src-pill{background:var(--card2);border-radius:3px;padding:.04rem .3rem}
.tag-pill{background:var(--tag-bg);color:var(--tag-text);border-radius:3px;padding:.04rem .3rem}

/* ── Bulk bar ─────────────────────────────────────────── */
.bulk-bar{background:var(--accent2);color:#fff;padding:.45rem 1rem;
          display:flex;align-items:center;gap:.65rem;font-size:.84rem;flex-shrink:0}
.bulk-bar.hidden{display:none}
.bulk-bar .btn{background:rgba(255,255,255,.15);color:#fff;border:1px solid rgba(255,255,255,.3)}
.bulk-bar .btn:hover{background:rgba(255,255,255,.28)}

/* ── Load more / empty ────────────────────────────────── */
.load-more-row{display:flex;justify-content:center;padding:1.2rem 0}
.empty{text-align:center;padding:3.5rem 2rem;color:var(--muted)}
.empty .icon{font-size:3.2rem;display:block;margin-bottom:.6rem}
.empty h3{color:var(--text);margin-bottom:.4rem}

/* ── Modal / Video.js container ───────────────────────── */
.modal-bg{display:none;position:fixed;inset:0;background:rgba(0,0,0,.9);
          z-index:100;align-items:flex-start;justify-content:center;
          padding:1rem;overflow-y:auto}
.modal-bg.open{display:flex;animation:fadeIn .15s ease}
.modal-bg.open .modal{animation:modalIn .18s ease}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
@keyframes modalIn{from{opacity:0;transform:translateY(8px) scale(.98)}to{opacity:1;transform:none}}
@media(prefers-reduced-motion:reduce){
  .modal-bg.open,.modal-bg.open .modal{animation:none}
}
.modal{background:var(--surface);border:1px solid var(--border);
       border-radius:14px;max-width:980px;width:100%;margin:auto;
       overflow:hidden;position:relative;box-shadow:0 20px 60px rgba(0,0,0,.45)}
.modal.theater{max-width:100%;border-radius:0;border:none;box-shadow:none}

.modal-close{position:absolute;top:.65rem;right:.65rem;background:rgba(0,0,0,.6);
             border:none;color:var(--muted);font-size:1.35rem;cursor:pointer;
             border-radius:50%;width:30px;height:30px;display:flex;
             align-items:center;justify-content:center;z-index:2;transition:.15s}
.modal-close:hover{background:rgba(0,0,0,.85);color:var(--text)}

/* Video.js customisation – match dark theme */
.video-wrap{background:#000;position:relative}
.video-js{width:100%!important;max-height:62vh}
.modal.theater .video-js{max-height:82vh}
.vjs-big-play-button{border-radius:50%!important;width:60px!important;
                     height:60px!important;line-height:60px!important;
                     border:2px solid rgba(255,255,255,.7)!important}
.video-js .vjs-control-bar{background:rgba(0,0,0,.75)!important;backdrop-filter:blur(4px)}
.video-js .vjs-play-progress,.video-js .vjs-volume-level{background:var(--accent)!important}
.video-js .vjs-slider:focus,.video-js button:focus{outline:none!important;box-shadow:none!important}

/* Prev/next navigation arrows over the video */
.vid-nav{position:absolute;top:50%;transform:translateY(-50%);
         width:100%;display:flex;justify-content:space-between;
         padding:0 .5rem;pointer-events:none;z-index:1}
.vid-nav-btn{background:rgba(0,0,0,.5);border:none;color:#fff;font-size:1.5rem;
             cursor:pointer;border-radius:50%;width:38px;height:38px;
             display:flex;align-items:center;justify-content:center;
             pointer-events:all;transition:.15s;backdrop-filter:blur(3px)}
.vid-nav-btn:hover{background:rgba(0,0,0,.85)}

.modal-body{padding:.9rem 1rem}
.modal-title{font-size:.98rem;font-weight:600;margin-bottom:.55rem;
             overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.meta-grid{display:grid;grid-template-columns:auto 1fr auto 1fr;gap:.28rem .75rem;
           font-size:.81rem;color:var(--muted);margin-bottom:.75rem}
.meta-grid span{color:var(--text)}
.modal-actions{display:flex;gap:.45rem;align-items:center;flex-wrap:wrap;margin-bottom:.75rem}
.tag-input{background:var(--card);border:1px solid var(--border);
           border-radius:var(--radius);padding:.3rem .6rem;color:var(--text);
           font-size:.82rem;width:165px}
.tag-input:focus{outline:none;border-color:var(--accent)}
.tag-list{display:flex;flex-wrap:wrap;gap:.3rem;margin-top:.35rem}
.tag-item{background:var(--tag-bg);color:var(--tag-text);border-radius:4px;
          padding:.16rem .42rem;font-size:.74rem;display:flex;align-items:center;gap:.25rem}
.tag-item .rm{cursor:pointer;opacity:.6;line-height:1}
.tag-item .rm:hover{opacity:1}
.kbd{background:var(--card2);border:1px solid var(--border);border-radius:4px;
     padding:.1rem .38rem;font-size:.7rem;font-family:monospace;color:var(--muted)}
.modal-options{display:flex;gap:1rem;font-size:.8rem;color:var(--muted);
               align-items:center;margin-top:.45rem;flex-wrap:wrap}
.modal-options label{display:flex;align-items:center;gap:.3rem;cursor:pointer}
.modal-options input[type=checkbox]{accent-color:var(--accent)}

/* ── Generic card (AI tab panels, suspicious-feed rows) ─ */
.card{background:var(--card);border:1px solid var(--border);
      border-radius:var(--radius);box-shadow:0 1px 3px rgba(0,0,0,.2);
      transition:.15s}
.card:hover{border-color:var(--accent2);box-shadow:0 4px 14px rgba(0,0,0,.28)}

/* ── Status page ──────────────────────────────────────── */
#page-status{overflow-y:auto;padding:1.5rem}
.status-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));
             gap:1rem;max-width:1050px;margin:0 auto;min-width:0}
.status-card{background:var(--card);border:1px solid var(--border);
             border-radius:var(--radius);padding:1rem 1.15rem;
             box-shadow:0 1px 3px rgba(0,0,0,.2);transition:.15s}
.status-card:hover{box-shadow:0 4px 14px rgba(0,0,0,.28)}
.status-card h3{font-size:.88rem;font-weight:600;margin-bottom:.75rem;
                display:flex;align-items:center;gap:.45rem}
.status-row{display:flex;justify-content:space-between;align-items:center;
            padding:.28rem 0;border-bottom:1px solid var(--border);font-size:.83rem}
.status-row:last-child{border-bottom:none}
.status-row .lbl{color:var(--muted)}
.status-row .val{color:var(--text);text-align:right;max-width:58%;
                 overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.val.ok{color:var(--success)} .val.warn{color:var(--warn)} .val.err{color:var(--danger)}
.prog-bar{background:var(--card2);border-radius:4px;height:5px;overflow:hidden;margin-top:.45rem}
.prog-fill{height:100%;background:var(--accent);border-radius:4px;transition:.5s}
.prog-fill.warn{background:var(--warn)} .prog-fill.danger{background:var(--danger)}

/* Activity chart */
.act-row{display:flex;align-items:center;gap:.6rem;padding:.18rem 0;font-size:.81rem}
.act-date{width:115px;color:var(--muted);flex-shrink:0;font-size:.75rem}
.act-bar-wrap{flex:1;background:var(--card2);border-radius:3px;height:13px;
              overflow:hidden;cursor:pointer;transition:.15s}
.act-bar-wrap:hover{filter:brightness(1.2)}
.act-bar{height:100%;background:var(--accent);border-radius:3px;
         transition:.35s;min-width:0}
.act-count{width:28px;text-align:right;color:var(--text);font-weight:600;font-size:.78rem}

/* ── Automations page ─────────────────────────────────── */
#page-automations,#page-ai,#page-usage{overflow-y:auto;padding:1.5rem}

/* ── AI Usage page ────────────────────────────────────── */
.usage-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(240px,100%),1fr));
            gap:1rem;margin-bottom:1.5rem}
.usage-stat{background:var(--card);border:1px solid var(--border);
            border-radius:var(--radius);padding:1.1rem 1.2rem;text-align:center}
.usage-stat .num{font-size:1.9rem;font-weight:700;color:var(--accent);
                 line-height:1.15;margin-bottom:.2rem}
.usage-stat .lbl{font-size:.76rem;color:var(--muted);text-transform:uppercase;
                 letter-spacing:.07em}
.table-scroll{overflow-x:auto;-webkit-overflow-scrolling:touch}
.usage-table{width:100%;border-collapse:collapse;font-size:.82rem;margin:.4rem 0 1.2rem}
.usage-table th{background:var(--card2);padding:.4rem .75rem;text-align:left;
                color:var(--muted);font-size:.73rem;font-weight:700;
                text-transform:uppercase;letter-spacing:.06em}
.usage-table td{padding:.4rem .75rem;border-bottom:1px solid var(--border)}
.usage-table tr:last-child td{border-bottom:none}
.usage-table tr:hover td{background:var(--card2)}
.auto-content{max-width:820px;margin:0 auto;min-width:0;width:100%}
.auto-content h2{font-size:1.08rem;font-weight:700;margin-bottom:.9rem}
.auto-content h3{font-size:.9rem;font-weight:600;color:var(--accent);
                 margin:.9rem 0 .35rem;display:flex;align-items:center;gap:.35rem}
.auto-content p,.auto-content li{font-size:.87rem;color:var(--muted);line-height:1.55}
.auto-content ul{padding-left:1.2rem;margin:.35rem 0}
.code-block{background:var(--card);border:1px solid var(--border);
            border-radius:var(--radius);padding:.9rem 1rem;font-family:monospace;
            font-size:.79rem;color:var(--code-color);line-height:1.5;overflow-x:auto;
            white-space:pre;position:relative;margin:.4rem 0 1.1rem}
.copy-btn{position:absolute;top:.4rem;right:.4rem;background:var(--card2);
          border:1px solid var(--border);color:var(--muted);
          border-radius:5px;padding:.18rem .5rem;font-size:.7rem;cursor:pointer}
.copy-btn:hover{color:var(--text)}
.event-table{width:100%;border-collapse:collapse;font-size:.8rem;margin:.4rem 0 1.1rem}
.event-table th{background:var(--card2);padding:.38rem .7rem;text-align:left;
                color:var(--muted);font-size:.73rem;font-weight:700;
                text-transform:uppercase;letter-spacing:.05em}
.event-table td{padding:.38rem .7rem;border-bottom:1px solid var(--border)}

/* ── AI analysis panel in clip modal ───────────────────── */
.ai-panel{border-top:1px solid var(--border);margin-top:.6rem;padding-top:.5rem}
.ai-panel-hdr{background:none;border:none;color:var(--muted);cursor:pointer;
              font-size:.8rem;display:flex;align-items:center;gap:.35rem;
              padding:.18rem 0;width:100%;text-align:left}
.ai-panel-hdr:hover{color:var(--accent)}
.ai-panel-hdr .chevron{margin-left:auto;font-size:.7rem;transition:transform .2s;display:inline-block}
.ai-panel-hdr.open .chevron{transform:rotate(90deg)}
.ai-panel-body{overflow:hidden;max-height:0;transition:max-height .28s ease}
.ai-panel-body.open{max-height:500px}
.ai-result-box{background:var(--card2);border:1px solid var(--border);
               border-radius:var(--radius);padding:.55rem .75rem;font-size:.82rem;
               margin-top:.4rem}
.ai-badge-suspicious{color:var(--danger);font-weight:700}
.ai-badge-clean{color:var(--success);font-weight:700}

/* ── Toast ────────────────────────────────────────────── */
.toast{position:fixed;bottom:1.4rem;right:1.4rem;background:#238636;
       color:#fff;padding:.55rem 1rem;border-radius:var(--radius);
       font-size:.84rem;z-index:500;opacity:0;transition:opacity .22s;
       pointer-events:none;max-width:min(310px,calc(100vw - 2.8rem))}
.toast.show{opacity:1}
.toast.err{background:var(--danger)}

/* ── Responsive ───────────────────────────────────────── */
@media(max-width:600px){
  .sidebar{display:none} .nav-tab span{display:none} .search{width:120px}
  .meta-grid{grid-template-columns:auto 1fr}

  /* The sidebar dashboard shell is a desktop affordance — a vertical nav
     would eat too much width from the content on a phone, so narrow
     viewports revert to the original horizontal top bar. */
  body{flex-direction:column}
  /* Unwrapped nav overflowed narrow viewports, clipping trailing buttons via body's overflow:hidden */
  .nav{flex-direction:row;flex-wrap:wrap;width:100%;height:auto;min-height:var(--nav-h);
       border-right:none;border-bottom:1px solid var(--border);
       align-items:center;padding:.4rem .6rem;row-gap:.35rem}
  .nav-brand{font-size:.92rem;margin-bottom:0}
  .nav-brand span{display:none}
  .nav-tabs{order:3;flex:1 1 100%;flex-direction:row;overflow-x:auto;
            -webkit-overflow-scrolling:touch;scrollbar-width:none}
  .nav-tabs::-webkit-scrollbar{display:none}
  .nav-tab{width:auto;padding:.4rem .8rem}
  .nav-actions{gap:.3rem;margin-top:0;padding-top:0;border-top:none}
  .nav-actions .btn.sm{padding:.3rem .55rem;font-size:.75rem}
}
</style>
</head>
<body>

<!-- Navigation -->
<nav class="nav">
  <div class="nav-brand">🎥 Blink <span>Clips</span></div>
  <div class="nav-tabs">
    <button class="nav-tab active" data-tab="library">📁 <span>Library</span></button>
    <button class="nav-tab" data-tab="status">📡 <span>Status</span></button>
    <button class="nav-tab" data-tab="usage">📊 <span>AI Usage</span></button>
    <button class="nav-tab" data-tab="automations">⚡ <span>Automations</span></button>
    <button class="nav-tab" data-tab="ai">🤖 <span>AI</span></button>
  </div>
  <div class="nav-actions">
    <button class="btn icon ghost" id="theme-btn" title="Toggle dark/light theme">🌙</button>
    <button class="btn icon ghost" id="help-btn" title="Keyboard shortcuts (?)">?</button>
    <button class="btn icon ghost" id="notif-btn" title="Enable notifications">🔕</button>
    <span id="conn-badge" class="badge">●</span>
    <button class="btn sm outline" id="refresh-btn">↻ Refresh</button>
    <button class="btn sm" id="sync-btn">⬇ Sync</button>
  </div>
</nav>

<!-- ── Library ──────────────────────────────────────────── -->
<div class="page active" id="page-library">
  <div class="lib-filters">
    <input class="search" id="search" type="search" placeholder="🔍 Search…">
    <select class="sel" id="date-range">
      <option value="">All time</option>
      <option value="today">Today</option>
      <option value="yesterday">Yesterday</option>
      <option value="week" selected>This week</option>
      <option value="month">This month</option>
    </select>
    <select class="sel" id="source-filter">
      <option value="">All sources</option>
      <option value="pir">Motion (PIR)</option>
      <option value="liveview">Liveview</option>
      <option value="snapshot">Snapshot</option>
    </select>
    <select class="sel" id="tag-filter">
      <option value="">All tags</option>
    </select>
    <select class="sel" id="sort-order">
      <option value="newest">⬆ Newest</option>
      <option value="oldest">⬇ Oldest</option>
      <option value="camera">📷 Camera</option>
      <option value="size">💾 Size</option>
      <option value="duration">⏱ Duration</option>
    </select>
    <label class="chk"><input type="checkbox" id="starred-only"> ★ Starred</label>
    <label class="chk"><input type="checkbox" id="notified-only"> 🔔 Notified</label>
    <button class="btn ghost sm" id="select-mode-btn">☐ Select</button>
  </div>

  <div class="bulk-bar hidden" id="bulk-bar">
    <span id="sel-count">0 selected</span>
    <button class="btn sm" id="bulk-star-btn">★ Star all</button>
    <button class="btn sm" id="bulk-delete-btn">🗑 Delete all</button>
    <button class="btn sm" id="bulk-zip-btn">⬇ ZIP</button>
    <button class="btn sm" style="margin-left:auto" id="bulk-cancel-btn">✕ Cancel</button>
  </div>

  <div class="lib-body">
    <aside class="sidebar">
      <div class="sb-head">Cameras</div>
      <div id="camera-nav">
        <div class="cam-item active" data-camera="all">
          All Cameras<span class="cam-badge" id="badge-all">—</span>
        </div>
      </div>
      <div class="sb-head" style="margin-top:.8rem">Storage</div>
      <div id="storage-info" style="padding:.4rem 1rem;font-size:.77rem;color:var(--muted)"></div>
    </aside>
    <main class="lib-main">
      <div class="stats-row" id="stats-bar"></div>
      <div class="clip-grid" id="clip-grid"></div>
      <div class="load-more-row">
        <button class="btn outline" id="load-more" style="display:none">Load more…</button>
      </div>
    </main>
  </div>
</div>

<!-- ── Status ───────────────────────────────────────────── -->
<div class="page" id="page-status">
  <div class="status-grid" id="status-grid"></div>
</div>

<!-- ── Automations ──────────────────────────────────────── -->
<div class="page" id="page-automations">
  <div class="auto-content">
    <h2>HA Automation Examples</h2>
    <p>The add-on fires events and updates a sensor every poll cycle. Copy these
       snippets into <code>automations.yaml</code> or the HA automation editor.</p>

    <h3>📡 Events &amp; Sensors</h3>
    <div class="table-scroll">
    <table class="event-table">
      <thead><tr><th>Type</th><th>Name</th><th>Description</th></tr></thead>
      <tbody>
        <tr><td>Sensor</td><td><code>sensor.blink_downloader_status</code></td>
            <td>Total clips; attributes: session_downloads, used_mb, free_gb, last_download</td></tr>
        <tr><td>Event</td><td><code>blink_clip_downloaded</code></td>
            <td>Per-clip event: clip_id, camera, path, timestamp, size_bytes, duration, source</td></tr>
      </tbody>
    </table>
    </div>

    <h3>⚡ Notify on any new clip</h3>
    <div class="code-block" id="auto1"><button class="copy-btn" data-target="auto1">Copy</button>alias: "Blink – new clip notification"
trigger:
  - platform: event
    event_type: blink_clip_downloaded
action:
  - service: notify.mobile_app_your_phone
    data:
      title: "🎥 New Blink clip – {{ trigger.event.data.camera }}"
      message: >
        {{ trigger.event.data.timestamp[:10] }}
        ({{ (trigger.event.data.size_bytes / 1048576) | round(1) }} MB)</div>

    <h3>⚡ Alert on long motion clip (&gt; 10 s)</h3>
    <div class="code-block" id="auto2"><button class="copy-btn" data-target="auto2">Copy</button>alias: "Blink – long motion clip"
trigger:
  - platform: event
    event_type: blink_clip_downloaded
condition:
  - condition: template
    value_template: >
      {{ trigger.event.data.source == 'pir' and
         (trigger.event.data.duration | int(0)) > 10 }}
action:
  - service: notify.notify
    data:
      title: "⚠️ Long motion clip"
      message: "{{ trigger.event.data.camera }} — {{ trigger.event.data.duration }}s"</div>

    <h3>⚡ Storage quota warning</h3>
    <div class="code-block" id="auto3"><button class="copy-btn" data-target="auto3">Copy</button>alias: "Blink – storage quota warning"
trigger:
  - platform: numeric_state
    entity_id: sensor.blink_downloader_status
    attribute: used_mb
    above: 8000
action:
  - service: notify.notify
    data:
      title: "💾 Blink storage nearing limit"
      message: >
        {{ state_attr('sensor.blink_downloader_status','used_mb')|int }} MB used.</div>

    <h3>⚡ Daily summary</h3>
    <div class="code-block" id="auto4"><button class="copy-btn" data-target="auto4">Copy</button>alias: "Blink – daily summary"
trigger:
  - platform: time
    at: "08:00:00"
action:
  - service: notify.notify
    data:
      title: "📅 Blink Daily Summary"
      message: >
        {{ states('sensor.blink_downloader_status') }} total clips.
        {{ state_attr('sensor.blink_downloader_status','session_downloads') }}
        downloaded this session.</div>

    <h3>💡 Tips</h3>
    <ul>
      <li>Enable <strong>Watch HA Events</strong> in add-on settings for instant download after motion.</li>
      <li>Tune <strong>Post-Motion Download Delay</strong> (default 30 s) to your Blink upload speed.</li>
      <li>Use <strong>⬇ Sync</strong> in the Library tab to trigger an immediate download cycle.</li>
      <li>Clips default to <code>/share/blink-clips/</code> — separate from HA's <code>/config/snapshots/</code>.</li>
      <li>The Video.js player supports keyboard shortcuts: <code>Space</code> play/pause,
          <code>← →</code> skip 10 s, <code>F</code> fullscreen, <code>M</code> mute,
          <code>↑ ↓</code> prev/next clip.</li>
    </ul>
  </div>
</div>

<!-- ── AI Usage ────────────────────────────────────────── -->
<div class="page" id="page-usage">
  <div class="auto-content">
    <h2 style="display:flex;align-items:center;justify-content:space-between;gap:1rem">
      <span>AI Token Usage</span>
      <button class="btn sm danger" id="usage-clear-btn">🗑 Clear Stats</button>
    </h2>
    <div id="usage-disabled-msg" style="display:none">
      <div class="status-card" style="padding:2rem;text-align:center;color:var(--muted)">
        <p style="font-size:1.2rem;margin-bottom:.8rem">📊 No AI Usage Data</p>
        <p>Enable AI analysis in the add-on settings. Usage statistics will appear after the first analysis run.</p>
      </div>
    </div>
    <div id="usage-content">
      <!-- Summary stats row -->
      <div class="usage-grid" id="usage-stats-grid">
        <div class="usage-stat"><div class="num" id="usage-total-analyses">—</div><div class="lbl">Clips Analyzed</div></div>
        <div class="usage-stat"><div class="num" id="usage-total-tokens">—</div><div class="lbl">Total Tokens</div></div>
        <div class="usage-stat"><div class="num" id="usage-prompt-tokens">—</div><div class="lbl">Prompt Tokens</div></div>
        <div class="usage-stat"><div class="num" id="usage-completion-tokens">—</div><div class="lbl">Completion Tokens</div></div>
        <div class="usage-stat" id="usage-escalations-stat" style="display:none"><div class="num" id="usage-escalations-value">—</div><div class="lbl">Escalations</div></div>
        <div class="usage-stat" id="usage-escalation-tokens-stat" style="display:none"><div class="num" id="usage-escalation-tokens-value">—</div><div class="lbl">Escalation Tokens</div></div>
        <div class="usage-stat" id="usage-cost-stat" style="display:none"><div class="num" id="usage-cost-value">—</div><div class="lbl">Estimated Cost</div></div>
      </div>

      <!-- Provider info card -->
      <div class="status-card" style="margin-bottom:1.2rem" id="usage-provider-card">
        <h3 style="margin-bottom:.75rem">Current Provider</h3>
        <div class="status-row"><span class="lbl">Provider</span><span class="val" id="usage-provider-name">—</span></div>
        <div class="status-row"><span class="lbl">Model</span><span class="val" id="usage-model-name">—</span></div>
        <div id="usage-provider-note" style="font-size:.8rem;color:var(--muted);margin-top:.6rem;line-height:1.55"></div>
      </div>

      <!-- Per-model breakdown table -->
      <h3 style="margin-bottom:.6rem">Per-Model Breakdown</h3>
      <div id="usage-model-table-wrap" class="table-scroll">
        <table class="usage-table">
          <thead>
            <tr>
              <th>Model</th>
              <th style="text-align:right">Analyses</th>
              <th style="text-align:right">Prompt Tokens</th>
              <th style="text-align:right">Completion Tokens</th>
              <th style="text-align:right">Total Tokens</th>
              <th style="text-align:right">Est. Cost</th>
            </tr>
          </thead>
          <tbody id="usage-model-tbody"></tbody>
        </table>
        <p id="usage-no-data" style="display:none;color:var(--muted);padding:1rem;text-align:center">No analysis data yet. Run the AI analysis to see usage statistics.</p>
      </div>

      <!-- Daily usage history -->
      <h3 style="margin-bottom:.6rem">Daily Usage (Last 14 Days)</h3>
      <div id="usage-daily-table-wrap" class="table-scroll">
        <table class="usage-table">
          <thead>
            <tr>
              <th>Date</th>
              <th style="text-align:right">Analyses</th>
              <th style="text-align:right">Total Tokens</th>
              <th style="text-align:right">Est. Cost</th>
            </tr>
          </thead>
          <tbody id="usage-daily-tbody"></tbody>
        </table>
        <p id="usage-daily-no-data" style="display:none;color:var(--muted);padding:1rem;text-align:center">No analysis activity in the last 14 days.</p>
      </div>
    </div>
  </div>
</div>

<!-- ── AI Analysis ─────────────────────────────────────── -->
<div class="page" id="page-ai">
  <div class="auto-content">
    <h2>AI Video Analysis</h2>
    <div id="ai-disabled-msg" style="display:none">
      <div class="card" style="padding:2rem;text-align:center;color:var(--muted)">
        <p style="font-size:1.2rem;margin-bottom:.8rem">🤖 AI Analysis Not Configured</p>
        <p>Enable AI analysis in the add-on settings and select a provider (Ollama, Moondream Cloud, or Moondream Local).</p>
      </div>
    </div>
    <div id="ai-content" style="display:none">
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(min(280px,100%),1fr));gap:1rem;margin-bottom:1.5rem">
        <!-- AI Connection Card -->
        <div class="card" style="padding:1.2rem">
          <h3 style="margin-bottom:.8rem">AI Connection</h3>
          <div style="display:flex;align-items:center;gap:.5rem;margin-bottom:.6rem">
            <span id="ai-status-badge" class="badge" style="font-size:.85rem">●</span>
            <span id="ai-status-text" style="font-size:.85rem">Checking…</span>
          </div>
          <div style="font-size:.82rem;color:var(--muted);margin-bottom:.4rem">
            Provider: <strong id="ai-provider-label">—</strong>
          </div>
          <div style="font-size:.82rem;color:var(--muted);margin-bottom:.6rem">
            Model: <strong id="ai-model-name">—</strong>
          </div>
          <div id="ai-escalation-info" style="display:none;font-size:.78rem;color:var(--muted);margin-bottom:.6rem;padding:.4rem .6rem;background:var(--card2);border-radius:var(--radius)">
            🪜 Escalation tier 2: <strong id="ai-escalation-label">—</strong>
            <span id="ai-escalation-status"></span>
          </div>
          <div id="ai-model-picker" style="display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
            <button class="btn sm" id="ai-fetch-models-btn">⟳ Fetch Models</button>
            <select class="sel" id="ai-model-select" style="min-width:175px">
              <option value="">Select a model…</option>
            </select>
            <button class="btn sm ghost" id="ai-copy-model-btn" title="Copy the selected model id, then paste it into this add-on's configuration (openai_model / anthropic_model / ollama_model)">📋 Copy</button>
          </div>
          <p style="font-size:.72rem;color:var(--muted);margin-top:.35rem">
            Selecting a model here does not change the running configuration — copy the
            id and paste it into the add-on's <strong>Configuration</strong> tab, then
            restart the add-on.
          </p>
          <!-- Moondream local: install / status section -->
          <div id="ai-moondream-local-section" style="display:none;margin-top:.75rem">
            <div id="ai-md-arch-unsupported" style="display:none">
              <p style="font-size:.8rem;color:var(--muted)">moondream_local is not available on this architecture.<br>Use <strong>moondream_cloud</strong> or <strong>ollama</strong> instead.</p>
            </div>
            <div id="ai-md-not-installed">
              <p style="font-size:.8rem;color:var(--warn);margin-bottom:.45rem">⚠ moondream package not installed</p>
              <button class="btn sm" id="ai-install-moondream-btn">⬇ Install Moondream 0.5B</button>
              <p style="font-size:.73rem;color:var(--muted);margin-top:.35rem">Package + model ~430 MB, may take several minutes</p>
            </div>
            <div id="ai-md-installing" style="display:none">
              <p style="font-size:.8rem;color:var(--warn);margin-bottom:.35rem">⏳ Installing… please wait</p>
              <div id="ai-md-install-log" style="font-size:.7rem;font-family:monospace;color:var(--muted);background:var(--card2);border:1px solid var(--border);border-radius:4px;padding:.3rem .5rem;max-height:70px;overflow-y:auto;white-space:pre-wrap"></div>
            </div>
            <div id="ai-md-installed" style="display:none">
              <p style="font-size:.8rem;color:var(--success)">✓ moondream installed</p>
              <p style="font-size:.73rem;color:var(--muted)">Model (~430 MB) downloads automatically on first health check</p>
            </div>
            <div id="ai-md-failed" style="display:none">
              <p style="font-size:.8rem;color:var(--danger);margin-bottom:.3rem">✗ Installation failed</p>
              <div id="ai-md-fail-log" style="font-size:.7rem;font-family:monospace;color:var(--muted);background:var(--card2);border:1px solid var(--border);border-radius:4px;padding:.3rem .5rem;max-height:70px;overflow-y:auto;white-space:pre-wrap;margin-bottom:.4rem"></div>
              <button class="btn sm ghost" id="ai-retry-moondream-btn">↺ Retry Install</button>
            </div>
          </div>
          <!-- Test analysis -->
          <div style="margin-top:.75rem;border-top:1px solid var(--border);padding-top:.6rem">
            <button class="btn sm ghost" id="ai-test-btn">🔬 Test Analysis</button>
            <p style="font-size:.73rem;color:var(--muted);margin:.3rem 0 0">Analyzes a recent clip to verify AI is working</p>
            <div id="ai-test-result" style="display:none;margin-top:.45rem"></div>
          </div>
        </div>
        <!-- Schedule Card -->
        <div class="card" style="padding:1.2rem">
          <h3 style="margin-bottom:.8rem">Schedule</h3>
          <div id="ai-schedule-info" style="font-size:.85rem;color:var(--muted)">Loading…</div>
        </div>
        <!-- Queue Status Card -->
        <div class="card" style="padding:1.2rem">
          <h3 style="margin-bottom:.8rem">Queue Status</h3>
          <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:.5rem">
            <div style="text-align:center">
              <div id="ai-q-pending" style="font-size:1.5rem;font-weight:700;color:var(--accent)">0</div>
              <div style="font-size:.72rem;color:var(--muted)">Pending</div>
            </div>
            <div style="text-align:center">
              <div id="ai-q-processing" style="font-size:1.5rem;font-weight:700;color:var(--warn)">0</div>
              <div style="font-size:.72rem;color:var(--muted)">Processing</div>
            </div>
            <div style="text-align:center">
              <div id="ai-q-completed" style="font-size:1.5rem;font-weight:700;color:var(--success)">0</div>
              <div style="font-size:.72rem;color:var(--muted)">Completed</div>
            </div>
            <div style="text-align:center">
              <div id="ai-q-failed" style="font-size:1.5rem;font-weight:700;color:var(--danger)">0</div>
              <div style="font-size:.72rem;color:var(--muted)">Failed</div>
            </div>
          </div>
        </div>
        <!-- Analysis Stats Card -->
        <div class="card" style="padding:1.2rem">
          <h3 style="margin-bottom:.8rem">Analysis Stats</h3>
          <div style="font-size:.85rem">
            <div>Total Analyzed: <strong id="ai-stat-total">0</strong></div>
            <div>Suspicious: <strong id="ai-stat-suspicious" style="color:var(--danger)">0</strong></div>
            <div style="color:var(--muted);font-size:.78rem;margin-top:.4rem">
              Last: <span id="ai-stat-last">—</span>
            </div>
          </div>
        </div>
        <!-- Notifications Card -->
        <div class="card" style="padding:1.2rem" id="ai-notifications-card">
          <h3 style="margin-bottom:.8rem">Email Alerts</h3>
          <div id="ai-smtp-not-configured" style="display:none;font-size:.82rem;color:var(--muted)">
            No SMTP settings configured — set <code>smtp_host</code> and
            <code>smtp_recipients</code> in the add-on's <strong>Configuration</strong> tab
            to enable email alerts.
          </div>
          <div id="ai-smtp-configured" style="display:none">
            <button class="btn sm ghost" id="ai-test-email-btn">✉️ Send Test Email</button>
            <p style="font-size:.73rem;color:var(--muted);margin:.3rem 0 0">
              Sends a one-off test email to verify SMTP settings, even if
              <code>smtp_enabled</code> is currently off.
            </p>
            <div id="ai-test-email-result" style="display:none;margin-top:.45rem;font-size:.8rem"></div>
          </div>
        </div>
        <!-- Adaptive Learning Card -->
        <div class="card" style="padding:1.2rem" id="ai-adaptive-card">
          <h3 style="margin-bottom:.8rem">🧠 Adaptive Learning</h3>
          <div id="ai-adaptive-content" style="font-size:.85rem">
            <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:.5rem;margin-bottom:.5rem">
              <div style="text-align:center">
                <div id="ai-fb-total" style="font-size:1.4rem;font-weight:700;color:var(--accent)">0</div>
                <div style="font-size:.7rem;color:var(--muted)">Feedback given</div>
              </div>
              <div style="text-align:center">
                <div id="ai-fb-accuracy" style="font-size:1.4rem;font-weight:700;color:var(--success)">—</div>
                <div style="font-size:.7rem;color:var(--muted)">Accuracy</div>
              </div>
            </div>
            <div id="ai-fb-breakdown" style="color:var(--muted);font-size:.78rem"></div>
            <p style="font-size:.72rem;color:var(--muted);margin-top:.5rem">
              Mark verdicts 👍/👎 on the Suspicious Activity Feed below, or in a clip's
              AI panel, to auto-tune per-camera alert thresholds and teach future
              analyses from your corrections.
            </p>
          </div>
        </div>
        <!-- Moondream Fine-Tuning Card (moondream_cloud only) -->
        <div class="card" style="padding:1.2rem;display:none" id="ai-finetune-card">
          <h3 style="margin-bottom:.8rem">🎯 Fine-Tuning</h3>
          <div id="ai-finetune-list" style="display:flex;flex-direction:column;gap:.4rem;margin-bottom:.6rem">
            <div style="color:var(--muted);font-size:.8rem">Loading…</div>
          </div>
          <div style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap">
            <input class="tag-input" id="ai-finetune-name" placeholder="New fine-tune name">
            <select class="sel" id="ai-finetune-rank">
              <option value="8">Rank 8</option>
              <option value="16" selected>Rank 16</option>
              <option value="24">Rank 24</option>
              <option value="32">Rank 32</option>
            </select>
            <button class="btn sm" id="ai-finetune-create-btn">+ New Fine-tune</button>
          </div>
          <p style="font-size:.72rem;color:var(--muted);margin-top:.4rem">
            Fine-tunes train in Moondream Cloud. Once a checkpoint finishes, activate it
            below to switch live inference immediately — no restart needed.
          </p>
        </div>
      </div>

      <!-- Camera Configurations -->
      <div style="margin-bottom:1.5rem">
        <h3 style="margin-bottom:.75rem;display:flex;align-items:center;gap:.5rem">
          📷 Camera Configurations
          <span style="font-size:.73rem;color:var(--muted);font-weight:400">
            — Set per-camera purpose and custom prompts
          </span>
        </h3>
        <div id="ai-car-protection-warning" style="display:none;background:rgba(245,158,11,.12);border:1px solid var(--warn,#f59e0b);border-radius:.4rem;padding:.6rem .8rem;font-size:.78rem;margin-bottom:.65rem">
          ⚠️ A camera below is marked "Protected vehicle visible from this camera", but no
          <strong>Protected Vehicle Description</strong> is set — car-proximity rules will not
          activate until you set one in the add-on's <strong>Configuration</strong> tab.
        </div>
        <div id="ai-cam-configs-loading" style="color:var(--muted);font-size:.85rem;padding:1rem">Loading…</div>
        <div id="ai-cam-configs-list" style="display:flex;flex-direction:column;gap:.65rem"></div>
        <div style="margin-top:.75rem;display:flex;gap:.5rem;align-items:center;flex-wrap:wrap">
          <button class="btn sm" id="ai-cam-save-btn">💾 Save Camera Configs</button>
          <span style="font-size:.75rem;color:var(--muted)">Changes apply immediately — no restart needed</span>
        </div>
      </div>

      <!-- Suspicious Activity Feed -->
      <h3 style="margin-bottom:.8rem">Suspicious Activity Feed</h3>
      <div id="ai-suspicious-feed" style="display:flex;flex-direction:column;gap:.5rem">
        <div style="color:var(--muted);padding:1rem;text-align:center">Loading…</div>
      </div>
    </div>
  </div>
</div>

<!-- ── Video player modal ─────────────────────────────── -->
<div class="modal-bg" id="modal-bg">
  <div class="modal" id="modal">
    <button class="modal-close" id="modal-close" title="Close (Esc)">×</button>
    <div class="video-wrap">
      <!-- Video.js – initialized once, source swapped per clip -->
      <video id="modal-video" class="video-js vjs-big-play-centered"
             preload="auto" playsinline>
        <p class="vjs-no-js">JavaScript is required to play videos.</p>
      </video>
      <div class="vid-nav">
        <button class="vid-nav-btn" id="vid-prev" title="Previous (↑)">‹</button>
        <button class="vid-nav-btn" id="vid-next" title="Next (↓)">›</button>
      </div>
    </div>
    <div class="modal-body">
      <div class="modal-title" id="modal-title"></div>
      <div class="meta-grid" id="modal-meta"></div>
      <div class="modal-actions">
        <button class="btn sm outline" id="star-btn">☆ Star</button>
        <a class="btn sm ghost" id="dl-link" download>⬇ Download</a>
        <button class="btn sm ghost" id="copy-path-btn">📋 Path</button>
        <button class="btn sm ghost" id="theater-btn" title="Theater mode">⊞</button>
        <button class="btn sm danger" id="delete-btn" style="margin-left:auto">🗑 Delete</button>
      </div>
      <div>
        <div style="display:flex;align-items:center;gap:.5rem;flex-wrap:wrap;margin-bottom:.3rem">
          <input class="tag-input" id="tag-input" placeholder="Add tag + Enter">
          <span style="font-size:.72rem;color:var(--muted)">
            <span class="kbd">Space</span> play &nbsp;
            <span class="kbd">←→</span> ±10s &nbsp;
            <span class="kbd">F</span> full &nbsp;
            <span class="kbd">M</span> mute &nbsp;
            <span class="kbd">↑↓</span> prev/next
          </span>
        </div>
        <div class="tag-list" id="tag-list"></div>
        <div class="modal-options">
          <label><input type="checkbox" id="autoplay-next"> Auto-play next clip</label>
          <label><input type="checkbox" id="loop-clip"> Loop</label>
        </div>
        <!-- AI Analysis panel – shown only when AI analysis is enabled -->
        <div id="modal-ai-panel" class="ai-panel" style="display:none">
          <button class="ai-panel-hdr" id="ai-panel-hdr">
            🤖 <strong style="font-weight:600">AI Analysis</strong>
            <span id="ai-panel-badge" style="font-size:.8rem"></span>
            <span class="chevron">▶</span>
          </button>
          <div class="ai-panel-body" id="ai-panel-body">
            <div id="ai-panel-content" style="padding-bottom:.3rem"></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ── AI prompt-debug overlay (only reachable when ai_prompt_debug_enabled) ── -->
<div class="modal-bg" id="prompt-overlay">
  <div class="modal" style="max-width:700px">
    <button class="modal-close" id="prompt-close" title="Close (Esc)">×</button>
    <div class="modal-body">
      <div class="modal-title" style="margin-bottom:.7rem">📝 Prompt Sent to AI</div>
      <p style="font-size:.78rem;color:var(--muted);margin-bottom:.6rem">
        The exact text sent to the model for this clip (image frames are not shown).
      </p>
      <pre id="prompt-overlay-content" style="font-size:.78rem;font-family:monospace;
           background:var(--card2);border:1px solid var(--border);border-radius:var(--radius);
           padding:.75rem .9rem;white-space:pre-wrap;word-break:break-word;
           max-height:60vh;overflow-y:auto;color:var(--text)"></pre>
    </div>
  </div>
</div>

<!-- ── Keyboard help overlay ────────────────────────────── -->
<div class="modal-bg" id="help-overlay">
  <div class="modal" style="max-width:460px">
    <button class="modal-close" id="help-close" title="Close (Esc)">×</button>
    <div class="modal-body">
      <div class="modal-title" style="margin-bottom:.9rem">⌨ Keyboard Shortcuts</div>
      <table style="width:100%;border-collapse:collapse;font-size:.83rem">
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.32rem .5rem;color:var(--muted)"><span class="kbd">Space</span></td><td style="padding:.32rem .5rem">Play / pause</td></tr>
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.32rem .5rem;color:var(--muted)"><span class="kbd">← →</span></td><td style="padding:.32rem .5rem">Seek ±10 s</td></tr>
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.32rem .5rem;color:var(--muted)"><span class="kbd">↑ ↓</span></td><td style="padding:.32rem .5rem">Previous / next clip</td></tr>
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.32rem .5rem;color:var(--muted)"><span class="kbd">F</span></td><td style="padding:.32rem .5rem">Toggle fullscreen</td></tr>
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.32rem .5rem;color:var(--muted)"><span class="kbd">M</span></td><td style="padding:.32rem .5rem">Toggle mute</td></tr>
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.32rem .5rem;color:var(--muted)"><span class="kbd">L</span></td><td style="padding:.32rem .5rem">Toggle loop</td></tr>
        <tr style="border-bottom:1px solid var(--border)"><td style="padding:.32rem .5rem;color:var(--muted)"><span class="kbd">Esc</span></td><td style="padding:.32rem .5rem">Close player or this overlay</td></tr>
        <tr><td style="padding:.32rem .5rem;color:var(--muted)"><span class="kbd">?</span></td><td style="padding:.32rem .5rem">Show / hide this overlay</td></tr>
      </table>
    </div>
  </div>
</div>

<!-- ── 2FA overlay (shown automatically when Blink requires verification) ── -->
<div class="modal-bg" id="twofa-overlay" style="z-index:200">
  <div class="modal" style="max-width:480px">
    <div class="modal-body" style="padding:1.8rem 1.6rem">
      <div class="modal-title" style="font-size:1.08rem;margin-bottom:.5rem">🔐 Two-Factor Authentication</div>
      <p style="color:var(--muted);font-size:.86rem;line-height:1.55;margin-bottom:1.2rem">
        Blink has sent a verification code to your registered email address or phone.
        Enter it below to complete sign-in.
      </p>
      <div style="display:flex;gap:.5rem;align-items:stretch">
        <input id="twofa-input"
               type="text"
               inputmode="numeric"
               pattern="[0-9]*"
               maxlength="6"
               placeholder="• • • • • •"
               autocomplete="one-time-code"
               style="flex:1;padding:.6rem .9rem;background:var(--card);
                      border:1px solid var(--border);border-radius:var(--radius);
                      color:var(--text);font-size:1.4rem;letter-spacing:.35em;
                      text-align:center;font-family:monospace;outline:none;transition:.15s"
               onfocus="this.style.borderColor='var(--accent)'"
               onblur="this.style.borderColor='var(--border)'">
        <button class="btn" id="twofa-submit" style="font-size:.9rem;padding:.6rem 1.1rem">
          Verify
        </button>
      </div>
      <div id="twofa-msg" style="margin-top:.75rem;font-size:.83rem;min-height:1.3rem;line-height:1.4"></div>
      <p style="color:var(--muted);font-size:.76rem;margin-top:1rem;line-height:1.45">
        Code not arriving? Check your spam folder or restart the add-on to request
        a new code. Codes expire quickly.
      </p>
    </div>
  </div>
</div>

<!-- ── Generic confirmation modal (replaces native confirm() for destructive actions) ── -->
<div class="modal-bg" id="confirm-overlay" style="z-index:200">
  <div class="modal" style="max-width:420px">
    <div class="modal-body" style="padding:1.6rem 1.5rem">
      <div class="modal-title" id="confirm-title" style="font-size:1.05rem;margin-bottom:.5rem">Are you sure?</div>
      <p id="confirm-message" style="color:var(--muted);font-size:.86rem;line-height:1.55;margin-bottom:1.3rem"></p>
      <div style="display:flex;gap:.6rem;justify-content:flex-end">
        <button class="btn sm ghost" id="confirm-cancel-btn">Cancel</button>
        <button class="btn sm danger" id="confirm-ok-btn">Confirm</button>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
'use strict';
// Ingress root prefix injected by server (empty for direct access, /api/hassio_ingress/TOKEN for ingress)
const _R = '__HAROOT__';

// ── Theme (dark default as of v4.0.0; manual override stored in localStorage) ─
// Dark is the shipped default regardless of OS/browser preference — an
// explicit stored choice (either way) is always honored, but unset means
// dark, not a prefers-color-scheme lookup.
(function(){
  const t = localStorage.getItem('blink_theme');
  if (t === 'light') document.body.classList.add('light');
  else document.body.classList.add('dark');
})();
function _isDark() {
  return !document.body.classList.contains('light');
}
function updateThemeBtn() {
  const btn = $('theme-btn');
  if (!btn) return;
  const dark = _isDark();
  btn.textContent = dark ? '☀' : '🌙';
  btn.title = dark ? 'Switch to light theme' : 'Switch to dark theme';
}
// ── State ──────────────────────────────────────────────────────────────────
let currentCamera = 'all', currentPage = 0, currentClipId = null, currentTags = [];
let selectMode = false, selectedIds = new Set();
let allClipIds = [];
let lastTotalCount = 0;
let notifEnabled = localStorage.getItem('blink_notif') === '1';
const PAGE_SIZE = 48;

// ── Video.js player instance (lazy-initialized) ───────────────────────────
let vPlayer = null;

function ensurePlayer() {
  if (vPlayer) return vPlayer;
  vPlayer = videojs('modal-video', {
    fluid: true,
    responsive: true,
    controls: true,
    // Start buffering immediately so playback doesn't stall on first click.
    preload: 'auto',
    playbackRates: [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2],
    // Reduces seek stalls: instead of jumping the decode pipeline to the
    // exact seek target, Video.js plays forward to it from the nearest
    // already-buffered keyframe — eliminates the "frozen frame" during seeks.
    enableSmoothSeeking: true,
    html5: {
      // For plain MP4 progressive downloads, use the browser's native video
      // stack rather than the VHS (HTTP Streaming) layer which is optimised
      // for HLS/DASH, not single-file MP4.  Native decoding on the Pi 5 can
      // also leverage hardware H.264 acceleration via V4L2/MMAL.
      vhs: { overrideNative: false },
      nativeVideoTracks: true,
      nativeAudioTracks: true,
      nativeTextTracks: true,
    },
    controlBar: {
      skipButtons: { forward: 10, backward: 10 },
      pictureInPictureToggle: true,
    },
    userActions: { hotkeys: false }, // handled manually below
  });

  // Loop toggle
  $('loop-clip').addEventListener('change', () => {
    vPlayer.loop($('loop-clip').checked);
  });

  // Auto-play next
  vPlayer.on('ended', () => {
    if ($('autoplay-next').checked) navClip(1);
  });

  return vPlayer;
}

// ── DOM helpers ────────────────────────────────────────────────────────────
const $ = id => document.getElementById(id);
function toast(msg, isErr = false, dur = 2800) {
  const el = $('toast');
  el.textContent = msg;
  el.classList.toggle('err', isErr);
  el.classList.add('show');
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.remove('show'), dur);
}

// ── Custom confirmation modal (replaces native confirm() for destructive actions) ──
let _confirmResolve = null;
function showConfirmModal(message, title = 'Are you sure?') {
  return new Promise(resolve => {
    $('confirm-title').textContent = title;
    $('confirm-message').textContent = message;
    _confirmResolve = resolve;
    $('confirm-overlay').classList.add('open');
  });
}
function _closeConfirmModal(result) {
  $('confirm-overlay').classList.remove('open');
  if (_confirmResolve) { _confirmResolve(result); _confirmResolve = null; }
}
$('confirm-ok-btn').addEventListener('click', () => _closeConfirmModal(true));
$('confirm-cancel-btn').addEventListener('click', () => _closeConfirmModal(false));
$('confirm-overlay').addEventListener('click', e => { if (e.target === $('confirm-overlay')) _closeConfirmModal(false); });

// ── Formatting ─────────────────────────────────────────────────────────────
function fmtSize(b) {
  if (!b) return '';
  if (b >= 1073741824) return (b / 1073741824).toFixed(2) + ' GB';
  if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB';
  return (b / 1024).toFixed(0) + ' KB';
}
function fmtDur(s) {
  if (!s) return '';
  const m = Math.floor(s / 60), sec = s % 60;
  return m ? `${m}m ${sec}s` : `${sec}s`;
}
function fmtTs(ts) {
  if (!ts) return '';
  try { return new Date(ts).toLocaleString(); } catch { return ts; }
}
function fmtRelative(ts) {
  if (!ts) return '';
  const d = (Date.now() - new Date(ts)) / 1000;
  if (d < 60) return 'just now';
  if (d < 3600) return Math.floor(d / 60) + 'm ago';
  if (d < 86400) return Math.floor(d / 3600) + 'h ago';
  return Math.floor(d / 86400) + 'd ago';
}
function sinceDate(range) {
  const d = new Date();
  if (range === 'today') { d.setHours(0, 0, 0, 0); }
  else if (range === 'yesterday') { d.setDate(d.getDate() - 1); d.setHours(0, 0, 0, 0); }
  else if (range === 'week') { d.setDate(d.getDate() - 7); }
  else if (range === 'month') { d.setDate(d.getDate() - 30); }
  else return null;
  return d.toISOString();
}

// ── API ────────────────────────────────────────────────────────────────────
async function api(path, opts = {}) {
  const r = await fetch(_R + path, opts);
  if (!r.ok) {
    const t = await r.text().catch(() => r.statusText);
    throw new Error(`${r.status}: ${t}`);
  }
  return r.json();
}

// ── Tab navigation ─────────────────────────────────────────────────────────
document.querySelectorAll('.nav-tab').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.nav-tab').forEach(b => b.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    btn.classList.add('active');
    $('page-' + btn.dataset.tab).classList.add('active');
    if (btn.dataset.tab === 'status') loadStatus();
  });
});

// ── Cameras sidebar ────────────────────────────────────────────────────────
async function loadCameras() {
  try {
    const cams = await api('/api/cameras');
    const total = cams.reduce((s, c) => s + (c.total || 0), 0);
    $('badge-all').textContent = total;
    const nav = $('camera-nav');
    const allEl = nav.querySelector('[data-camera="all"]');
    nav.innerHTML = '';
    nav.appendChild(allEl);
    cams.forEach(c => {
      const el = document.createElement('div');
      el.className = 'cam-item' + (c.camera === currentCamera ? ' active' : '');
      el.dataset.camera = c.camera;
      el.innerHTML = `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">${_esc(c.camera)}</span>`
        + `<span class="cam-badge">${_esc(String(c.total || 0))}</span>`;
      nav.appendChild(el);
    });
    nav.querySelectorAll('.cam-item').forEach(el => el.addEventListener('click', () => {
      currentCamera = el.dataset.camera;
      nav.querySelectorAll('.cam-item').forEach(x => x.classList.remove('active'));
      el.classList.add('active');
      currentPage = 0; $('clip-grid').innerHTML = ''; loadClips(0);
    }));
  } catch (e) { console.warn('loadCameras', e); }
}

// ── Stats bar ──────────────────────────────────────────────────────────────
async function loadStats() {
  try {
    const s = await api('/api/stats');
    const bar = $('stats-bar');
    bar.innerHTML =
      `<div class="stat-chip">Today <strong>${s.today_count ?? 0}</strong></div>` +
      `<div class="stat-chip">Week <strong>${s.week_count ?? 0}</strong></div>` +
      `<div class="stat-chip">Total <strong>${s.total_count ?? 0}</strong></div>` +
      `<div class="stat-chip">★ Starred <strong>${s.starred_count ?? 0}</strong></div>` +
      `<div class="stat-chip">Library <strong>${((s.total_size_bytes ?? 0) / 1073741824).toFixed(2)} GB</strong></div>`;

    if (s.disk) {
      const pct = s.disk.quota_bytes
        ? Math.min(100, (s.disk.used_bytes / s.disk.quota_bytes) * 100) : 0;
      const cls = pct > 90 ? 'danger' : pct > 70 ? 'warn' : '';
      $('storage-info').innerHTML =
        `<div>Used: ${s.disk.used_mb} MB</div>` +
        `<div style="color:var(--text)">Free: ${s.disk.free_gb} GB</div>` +
        (s.disk.quota_bytes
          ? `<div class="prog-bar" style="margin-top:.35rem"><div class="prog-fill ${cls}" style="width:${pct.toFixed(1)}%"></div></div>`
          : '');
    }

    // Connection badge
    const badge = $('conn-badge');
    if (s.connected != null) {
      badge.className = 'badge ' + (s.connected ? 'ok' : 'err');
      badge.textContent = s.connected ? '● Connected' : '● Disconnected';
    }

    // Browser notifications for new clips
    const total = s.total_count || 0;
    if (lastTotalCount > 0 && total > lastTotalCount && notifEnabled
        && Notification.permission === 'granted') {
      const n = total - lastTotalCount;
      new Notification(`🎥 ${n} new Blink clip${n > 1 ? 's' : ''}`, {
        body: 'New clips are available in your library.',
        tag: 'blink-new-clips',
      });
    }
    lastTotalCount = total;
  } catch (e) { console.warn('loadStats', e); }
}

// ── Tag filter ─────────────────────────────────────────────────────────────
async function loadTagFilter() {
  try {
    const tags = await api('/api/tags');
    const sel = $('tag-filter');
    if (sel.options.length > 1) return; // already populated
    tags.forEach(t => {
      const opt = document.createElement('option');
      opt.value = t; opt.textContent = '#' + t;
      sel.appendChild(opt);
    });
  } catch (e) {}
}

// ── Notifications button ───────────────────────────────────────────────────
function updateNotifBtn() {
  const btn = $('notif-btn');
  if (!('Notification' in window)) { btn.style.display = 'none'; return; }
  btn.textContent = notifEnabled ? '🔔' : '🔕';
  btn.title = notifEnabled ? 'Notifications ON (click to disable)' : 'Enable browser notifications';
}
$('notif-btn').addEventListener('click', async () => {
  if (!notifEnabled) {
    const perm = await Notification.requestPermission();
    if (perm === 'granted') {
      notifEnabled = true; localStorage.setItem('blink_notif', '1');
      toast('Browser notifications enabled 🔔');
    } else {
      toast('Notification permission denied', true);
    }
  } else {
    notifEnabled = false; localStorage.removeItem('blink_notif');
    toast('Notifications disabled');
  }
  updateNotifBtn();
});
updateNotifBtn();

// ── Theme toggle ───────────────────────────────────────────────────────────
$('theme-btn').addEventListener('click', () => {
  const wasDark = _isDark();
  document.body.classList.remove('dark', 'light');
  document.body.classList.add(wasDark ? 'light' : 'dark');
  localStorage.setItem('blink_theme', wasDark ? 'light' : 'dark');
  updateThemeBtn();
});
updateThemeBtn();

// ── Clip card ──────────────────────────────────────────────────────────────
function buildCard(c) {
  const div = document.createElement('div');
  div.className = 'clip-card' + (selectedIds.has(c.id) ? ' selected' : '');
  div.dataset.id = c.id;
  div.innerHTML =
    `<div class="thumb-wrap">` +
    `<img src="${_R}/api/clips/${c.id}/thumb" loading="lazy" alt="" `
    + `onerror="this.style.display='none';this.nextSibling.style.display='flex'">` +
    `<div class="no-thumb" style="display:none">🎬</div>` +
    (c.duration ? `<div class="dur-badge">${fmtDur(c.duration)}</div>` : '') +
    (c.starred ? '<div class="star-badge">★</div>' : '') +
    (c.notified ? '<div class="notified-badge">🔔</div>' : '') +
    `<div class="sel-check">${selectedIds.has(c.id) ? '✓' : ''}</div>` +
    `</div>` +
    `<div class="clip-info">` +
    `<div class="clip-camera">${_esc(c.camera)}</div>` +
    `<div class="clip-time">${fmtTs(c.timestamp)}</div>` +
    `<div class="clip-meta">` +
    (c.source ? `<span class="src-pill">${_esc(c.source)}</span>` : '') +
    `<span>${fmtSize(c.size_bytes)}</span>` +
    (c.tags || []).map(t => `<span class="tag-pill">${_esc(t)}</span>`).join('') +
    `</div></div>`;
  div.addEventListener('click', () => {
    if (selectMode) { toggleSelect(c.id, div); return; }
    openModal(c.id);
  });
  return div;
}

// ── Load clips ─────────────────────────────────────────────────────────────
async function loadClips(page = 0) {
  const grid = $('clip-grid');
  if (page === 0) { grid.innerHTML = '<div style="grid-column:1/-1;padding:2rem;text-align:center;color:var(--muted)">Loading…</div>'; allClipIds = []; }
  const p = new URLSearchParams({ limit: PAGE_SIZE, offset: page * PAGE_SIZE });
  if (currentCamera !== 'all') p.set('camera', currentCamera);
  const sr = $('search').value.trim(); if (sr) p.set('search', sr);
  const dr = sinceDate($('date-range').value); if (dr) p.set('since', dr);
  if ($('starred-only').checked) p.set('starred', '1');
  if ($('notified-only').checked) p.set('notified', '1');
  const src = $('source-filter').value; if (src) p.set('source', src);
  const tf = $('tag-filter').value; if (tf) p.set('tag', tf);
  p.set('sort', $('sort-order').value || 'newest');
  try {
    const clips = await api(`/api/clips?${p}`);
    if (page === 0) grid.innerHTML = '';
    if (!clips.length && page === 0) {
      grid.innerHTML = '<div class="empty"><span class="icon">📭</span><h3>No clips found</h3><p>Try adjusting filters or tap Sync to fetch new clips.</p></div>';
      $('load-more').style.display = 'none'; return;
    }
    clips.forEach(c => { allClipIds.push(c.id); grid.appendChild(buildCard(c)); });
    $('load-more').style.display = clips.length < PAGE_SIZE ? 'none' : 'inline-flex';
    currentPage = page;
  } catch (e) { toast('Failed to load clips', true); console.error(e); }
}

// ── Selection ──────────────────────────────────────────────────────────────
function toggleSelectMode(on) {
  selectMode = on; selectedIds.clear();
  $('bulk-bar').classList.toggle('hidden', !on);
  $('select-mode-btn').textContent = on ? '☒ Selecting' : '☐ Select';
  updateBulkBar();
  document.querySelectorAll('.clip-card').forEach(el => {
    el.querySelector('.sel-check').textContent = '';
    el.classList.remove('selected');
  });
}
function toggleSelect(id, el) {
  if (selectedIds.has(id)) { selectedIds.delete(id); el.classList.remove('selected'); el.querySelector('.sel-check').textContent = ''; }
  else { selectedIds.add(id); el.classList.add('selected'); el.querySelector('.sel-check').textContent = '✓'; }
  updateBulkBar();
}
function updateBulkBar() { $('sel-count').textContent = `${selectedIds.size} selected`; }
$('select-mode-btn').addEventListener('click', () => toggleSelectMode(!selectMode));
$('bulk-cancel-btn').addEventListener('click', () => toggleSelectMode(false));
$('bulk-star-btn').addEventListener('click', async () => {
  if (!selectedIds.size) return;
  await Promise.all([...selectedIds].map(id => api(`/api/clips/${id}/star`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ starred: true }) })));
  toast(`Starred ${selectedIds.size} clip(s)`);
  toggleSelectMode(false); loadClips(0);
});
$('bulk-delete-btn').addEventListener('click', async () => {
  if (!selectedIds.size || !confirm(`Delete ${selectedIds.size} clip(s) permanently?`)) return;
  await Promise.all([...selectedIds].map(id => api(`/api/clips/${id}`, { method: 'DELETE' }).catch(() => {})));
  toast(`Deleted ${selectedIds.size} clip(s)`);
  toggleSelectMode(false); loadClips(0); loadStats();
});
$('bulk-zip-btn').addEventListener('click', async () => {
  if (!selectedIds.size) return;
  const btn = $('bulk-zip-btn'); btn.disabled = true; btn.textContent = '⏳ Zipping…';
  try {
    const resp = await fetch(_R + '/api/clips/export-zip', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ids: [...selectedIds] }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'blink-clips.zip'; a.click();
    URL.revokeObjectURL(url);
    toast(`Downloaded ${selectedIds.size} clip(s) as ZIP`);
  } catch (e) { toast('ZIP export failed', true); console.error(e); }
  finally { btn.disabled = false; btn.textContent = '⬇ ZIP'; }
});

// ── Video player / modal ───────────────────────────────────────────────────
async function openModal(clipId) {
  currentClipId = clipId;
  const player = ensurePlayer();
  try {
    const c = await api(`/api/clips/${clipId}`);
    currentTags = [...(c.tags || [])];

    // Update Video.js source — no page reload needed
    player.src([{ src: `${_R}/api/clips/${clipId}/stream`, type: 'video/mp4' }]);
    player.load();

    $('modal-title').textContent = `${c.camera} — ${fmtTs(c.timestamp)}`;
    $('modal-meta').innerHTML =
      `<div>Camera</div><span>${_esc(c.camera)}</span>` +
      `<div>Recorded</div><span>${fmtTs(c.timestamp)}</span>` +
      `<div>Duration</div><span>${fmtDur(c.duration) || '—'}</span>` +
      `<div>Size</div><span>${fmtSize(c.size_bytes) || '—'}</span>` +
      `<div>Source</div><span>${_esc(c.source || '—')}</span>` +
      `<div>Added</div><span>${fmtRelative(c.downloaded_at)}</span>`;
    updateStarBtn(c.starred);
    const dl = $('dl-link');
    dl.href = `${_R}/api/clips/${clipId}/stream`;
    dl.download = `${c.camera}_${(c.timestamp || '').replace(/[:.]/g, '-')}.mp4`;
    $('copy-path-btn').dataset.path = c.file_path || '';
    renderTags();

    $('modal-bg').classList.add('open');
    // Reset and optionally show the AI analysis panel
    const aiPanel = $('modal-ai-panel');
    const aiPanelBody = $('ai-panel-body');
    const aiPanelHdr = $('ai-panel-hdr');
    if (aiPanel) aiPanel.style.display = _aiEnabled ? 'block' : 'none';
    if (aiPanelBody) { aiPanelBody.classList.remove('open'); delete aiPanelBody.dataset.loaded; }
    if (aiPanelHdr) aiPanelHdr.classList.remove('open');
    const aiPanelBadge = $('ai-panel-badge');
    if (aiPanelBadge) { aiPanelBadge.textContent = ''; aiPanelBadge.style.color = ''; }
    // Attempt auto-play (may be blocked by browser autoplay policy)
    player.play().catch(() => {});
  } catch (e) { toast('Failed to load clip', true); console.error(e); }
}

function closeModal() {
  if (vPlayer) { vPlayer.pause(); vPlayer.src(''); }
  $('modal-bg').classList.remove('open');
  currentClipId = null;
}
function updateStarBtn(starred) {
  const btn = $('star-btn');
  btn.textContent = starred ? '★ Starred' : '☆ Star';
  btn.style.color = starred ? 'var(--starred)' : '';
  btn.dataset.starred = starred ? '1' : '0';
}
function renderTags() {
  const list = $('tag-list');
  list.innerHTML = currentTags.map(t =>
    `<span class="tag-item">${_esc(t)}<span class="rm" data-tag="${_esc(t)}">×</span></span>`
  ).join('');
  list.querySelectorAll('.rm').forEach(el => el.addEventListener('click', async () => {
    currentTags = currentTags.filter(t => t !== el.dataset.tag);
    await saveTags(); renderTags();
  }));
}
async function saveTags() {
  if (!currentClipId) return;
  await api(`/api/clips/${currentClipId}/tags`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ tags: currentTags }),
  });
}
function navClip(dir) {
  const idx = allClipIds.indexOf(currentClipId);
  const next = idx + dir;
  if (next >= 0 && next < allClipIds.length) openModal(allClipIds[next]);
}

// Modal event listeners
$('modal-close').addEventListener('click', closeModal);
$('modal-bg').addEventListener('click', e => { if (e.target === $('modal-bg')) closeModal(); });
$('vid-prev').addEventListener('click', () => navClip(-1));
$('vid-next').addEventListener('click', () => navClip(1));

$('theater-btn').addEventListener('click', () => {
  const m = $('modal'); m.classList.toggle('theater');
  $('theater-btn').textContent = m.classList.contains('theater') ? '⊡ Normal' : '⊞ Theater';
  vPlayer && vPlayer.fluid(!m.classList.contains('theater'));
});

$('star-btn').addEventListener('click', async () => {
  if (!currentClipId) return;
  const starred = $('star-btn').dataset.starred !== '1';
  await api(`/api/clips/${currentClipId}/star`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ starred }),
  });
  updateStarBtn(starred);
  toast(starred ? 'Starred ★' : 'Unstarred');
  document.querySelectorAll(`.clip-card[data-id="${currentClipId}"]`).forEach(el => {
    const b = el.querySelector('.star-badge');
    if (starred && !b) { const nb = document.createElement('div'); nb.className = 'star-badge'; nb.textContent = '★'; el.querySelector('.thumb-wrap').prepend(nb); }
    else if (!starred && b) b.remove();
  });
});

$('delete-btn').addEventListener('click', async () => {
  if (!currentClipId || !confirm('Delete this clip permanently?')) return;
  const id = currentClipId;
  await api(`/api/clips/${id}`, { method: 'DELETE' });
  toast('Clip deleted');
  document.querySelector(`.clip-card[data-id="${id}"]`)?.remove();
  const idx = allClipIds.indexOf(id);
  if (idx !== -1) allClipIds.splice(idx, 1);
  // Open whichever clip now occupies the same slot, fall back to the
  // previous one, or close the modal if the list is empty.
  if (idx >= 0 && idx < allClipIds.length) openModal(allClipIds[idx]);
  else if (idx - 1 >= 0) openModal(allClipIds[idx - 1]);
  else closeModal();
});

$('copy-path-btn').addEventListener('click', () => {
  const path = $('copy-path-btn').dataset.path;
  if (path) navigator.clipboard.writeText(path)
    .then(() => toast('File path copied'))
    .catch(() => toast(path, true));
});

$('tag-input').addEventListener('keydown', async e => {
  if (e.key === 'Enter') {
    const v = e.target.value.trim().toLowerCase().replace(/[^a-z0-9_-]/g, '');
    if (v && !currentTags.includes(v)) { currentTags.push(v); await saveTags(); renderTags(); }
    e.target.value = '';
  }
});

// ── Keyboard shortcuts (Video.js API) ─────────────────────────────────────
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  if (e.key === '?') { $('help-overlay').classList.toggle('open'); return; }
  if (e.key === 'Escape') {
    if ($('help-overlay').classList.contains('open')) { $('help-overlay').classList.remove('open'); return; }
    if ($('prompt-overlay').classList.contains('open')) { $('prompt-overlay').classList.remove('open'); return; }
    if ($('confirm-overlay').classList.contains('open')) { _closeConfirmModal(false); return; }
    if ($('modal-bg').classList.contains('open')) { closeModal(); }
    return;
  }
  if (!$('modal-bg').classList.contains('open')) return;
  const p = vPlayer;
  switch (e.key) {
    case ' ':         e.preventDefault(); p && (p.paused() ? p.play() : p.pause()); break;
    case 'ArrowLeft': e.preventDefault(); p && p.currentTime(Math.max(0, p.currentTime() - 10)); break;
    case 'ArrowRight':e.preventDefault(); p && p.currentTime(Math.min(p.duration() || 0, p.currentTime() + 10)); break;
    case 'f': case 'F': p && p.requestFullscreen(); break;
    case 'm': case 'M': p && p.muted(!p.muted()); break;
    case 'ArrowUp':   e.preventDefault(); navClip(-1); break;
    case 'ArrowDown': e.preventDefault(); navClip(1); break;
    case 'l': case 'L': {
      const cb = $('loop-clip'); cb.checked = !cb.checked; p && p.loop(cb.checked);
      toast(cb.checked ? 'Loop ON' : 'Loop OFF'); break;
    }
  }
});

// ── Sync / refresh ─────────────────────────────────────────────────────────
$('sync-btn').addEventListener('click', async () => {
  const btn = $('sync-btn'); btn.disabled = true; btn.textContent = '⏳ Syncing…';
  try {
    await api('/api/download-now', { method: 'POST' });
    toast('Download triggered — clips appear shortly');
    setTimeout(() => { currentPage = 0; loadAll(); }, 10000);
  } catch { toast('Sync failed', true); }
  finally { setTimeout(() => { btn.disabled = false; btn.textContent = '⬇ Sync'; }, 3000); }
});
$('refresh-btn').addEventListener('click', () => { currentPage = 0; loadAll(); });
$('load-more').addEventListener('click', () => loadClips(currentPage + 1));

// Help overlay wiring
$('help-btn').addEventListener('click', () => $('help-overlay').classList.toggle('open'));
$('help-close').addEventListener('click', () => $('help-overlay').classList.remove('open'));
$('help-overlay').addEventListener('click', e => { if (e.target === $('help-overlay')) $('help-overlay').classList.remove('open'); });

// AI prompt-debug overlay wiring
function showPromptModal(promptText) {
  $('prompt-overlay-content').textContent = promptText || 'No prompt was captured for this clip.';
  $('prompt-overlay').classList.add('open');
}
$('prompt-close').addEventListener('click', () => $('prompt-overlay').classList.remove('open'));
$('prompt-overlay').addEventListener('click', e => { if (e.target === $('prompt-overlay')) $('prompt-overlay').classList.remove('open'); });

// Debounced filter listeners
let _dbt;
['search', 'date-range', 'starred-only', 'notified-only', 'source-filter', 'tag-filter', 'sort-order'].forEach(id => {
  $(id).addEventListener(id === 'search' ? 'input' : 'change', () => {
    clearTimeout(_dbt); _dbt = setTimeout(() => { currentPage = 0; $('clip-grid').innerHTML = ''; loadClips(0); }, 380);
  });
});

// ── Status page ────────────────────────────────────────────────────────────
async function loadStatus() {
  const grid = $('status-grid');
  grid.innerHTML = '<div style="padding:2rem;color:var(--muted)">Loading…</div>';
  try {
    const [stats, cams, actData, aiData] = await Promise.all([
      api('/api/stats'), api('/api/cameras'), api('/api/activity?days=7'),
      api('/api/ai/status').catch(() => null),
    ]);
    let html = '';

    // Connection
    const conn = stats.connected;
    html += `<div class="status-card"><h3>📡 Blink Connection</h3>`
      + `<div class="status-row"><span class="lbl">Status</span><span class="val ${conn ? 'ok' : 'err'}">${conn ? 'Connected' : 'Disconnected'}</span></div>`
      + (stats.account_id ? `<div class="status-row"><span class="lbl">Account ID</span><span class="val">${stats.account_id}</span></div>` : '')
      + (stats.last_download ? `<div class="status-row"><span class="lbl">Last download</span><span class="val">${fmtTs(stats.last_download)}</span></div>` : '')
      + `</div>`;

    // Library
    html += `<div class="status-card"><h3>📚 Clip Library</h3>`
      + `<div class="status-row"><span class="lbl">Total clips</span><span class="val">${stats.total_count ?? 0}</span></div>`
      + `<div class="status-row"><span class="lbl">Today</span><span class="val">${stats.today_count ?? 0}</span></div>`
      + `<div class="status-row"><span class="lbl">This week</span><span class="val">${stats.week_count ?? 0}</span></div>`
      + `<div class="status-row"><span class="lbl">Starred</span><span class="val">${stats.starred_count ?? 0}</span></div>`
      + `<div class="status-row"><span class="lbl">Archived</span><span class="val">${stats.archived_count ?? 0}</span></div>`
      + `</div>`;

    // Storage
    if (stats.disk) {
      const d = stats.disk;
      const pct = d.quota_bytes ? Math.min(100, (d.used_bytes / d.quota_bytes) * 100) : null;
      const cls = pct && pct > 90 ? 'danger' : pct && pct > 70 ? 'warn' : '';
      html += `<div class="status-card"><h3>💾 Storage</h3>`
        + `<div class="status-row"><span class="lbl">Used</span><span class="val ${cls || 'ok'}">${d.used_mb} MB</span></div>`
        + `<div class="status-row"><span class="lbl">Free (disk)</span><span class="val">${d.free_gb} GB</span></div>`
        + (d.quota_bytes ? `<div class="status-row"><span class="lbl">Quota</span><span class="val">${d.quota_gb} GB</span></div>`
          + `<div class="prog-bar"><div class="prog-fill ${cls}" style="width:${(pct || 0).toFixed(1)}%"></div></div>` : '')
        + `</div>`;
    }

    // Frames analyzed
    const frameStats = (aiData && aiData.analysis_stats) || {};
    if (frameStats.total_frames_analyzed) {
      html += `<div class="status-card"><h3>🖼️ Frames Analyzed</h3>`
        + `<div class="status-row"><span class="lbl">Total frames</span><span class="val">${frameStats.total_frames_analyzed}</span></div>`
        + `<div class="status-row"><span class="lbl">Today</span><span class="val">${frameStats.frames_analyzed_today || 0}</span></div>`
        + `</div>`;
    }

    // Cameras
    if (cams.length) {
      html += `<div class="status-card"><h3>📷 Cameras (${cams.length})</h3>`;
      cams.forEach(c => {
        html += `<div class="status-row"><span class="lbl">${_esc(c.camera)}</span>`
          + `<span class="val">${c.total || 0} clips — ${c.today || 0} today</span></div>`;
      });
      html += `</div>`;
    }

    // AI Analysis status card
    if (aiData && aiData.enabled) {
      const provNames = {ollama:'Ollama (Local)',ollama_cloud:'Ollama Cloud',moondream_cloud:'Moondream Cloud',moondream_local:'Moondream Local (0.5B)',anthropic:'Anthropic (Claude)',openai:'OpenAI (GPT)'};
      const prov = aiData.provider || 'ollama';
      const provLabel = provNames[prov] || prov;
      const online = aiData.ai_online;
      const qs = aiData.queue || {};
      const as_ = frameStats;
      html += `<div class="status-card"><h3>🤖 AI Analysis</h3>`
        + `<div class="status-row"><span class="lbl">Status</span><span class="val ${online ? 'ok' : 'err'}">${online ? 'Online' : 'Offline'}</span></div>`
        + `<div class="status-row"><span class="lbl">Provider</span><span class="val">${_esc(provLabel)}</span></div>`
        + `<div class="status-row"><span class="lbl">Model</span><span class="val">${_esc(aiData.model||'—')}</span></div>`
        + (qs.pending !== undefined ? `<div class="status-row"><span class="lbl">Pending</span><span class="val">${qs.pending||0}</span></div>` : '')
        + (as_.total_analyzed ? `<div class="status-row"><span class="lbl">Analyzed</span><span class="val">${as_.total_analyzed}</span></div>` : '')
        + (as_.suspicious_count ? `<div class="status-row"><span class="lbl">Suspicious</span><span class="val" style="color:var(--danger)">${as_.suspicious_count}</span></div>` : '')
        + `</div>`;
    }

    // Activity chart (full-width card)
    html += `<div class="status-card" style="grid-column:1/-1"><h3>📅 Activity — last 7 days</h3>`
      + `<div id="act-chart"></div></div>`;

    grid.innerHTML = html;

    // Render activity chart
    renderActivity(actData);
  } catch (e) {
    grid.innerHTML = '<div style="padding:2rem;color:var(--danger)">Failed to load status.</div>';
  }
}

function renderActivity(rows) {
  const container = $('act-chart');
  if (!container) return;
  if (!rows.length) { container.innerHTML = '<p style="color:var(--muted);font-size:.84rem">No recent activity.</p>'; return; }

  // Group by date
  const byDate = {};
  rows.forEach(({ date, hour, count }) => {
    if (!byDate[date]) byDate[date] = { total: 0, hours: {} };
    byDate[date].total += count;
    byDate[date].hours[hour] = count;
  });

  const dates = Object.keys(byDate).sort().reverse();
  const maxCount = Math.max(...dates.map(d => byDate[d].total), 1);

  container.innerHTML = dates.map(date => {
    const { total } = byDate[date];
    const pct = (total / maxCount) * 100;
    const d = new Date(date + 'T12:00:00');
    const label = d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
    return `<div class="act-row">
      <span class="act-date">${label}</span>
      <div class="act-bar-wrap" title="${total} clips" onclick="filterByDate('${_escJs(date)}')">
        <div class="act-bar" style="width:${pct.toFixed(1)}%"></div>
      </div>
      <span class="act-count">${total}</span>
    </div>`;
  }).join('');
}

// Click on activity bar → filter library to that day
function filterByDate(date) {
  // Switch to library tab and set date filter
  document.querySelector('[data-tab="library"]').click();
  $('date-range').value = 'custom_' + date;
  // Set since/until manually via custom query
  currentPage = 0; $('clip-grid').innerHTML = '';
  const params = new URLSearchParams({ limit: PAGE_SIZE, offset: 0 });
  params.set('since', date + 'T00:00:00Z');
  params.set('until', date + 'T23:59:59Z');
  api(`/api/clips?${params}`).then(clips => {
    const grid = $('clip-grid');
    grid.innerHTML = '';
    if (!clips.length) { grid.innerHTML = '<div class="empty"><span class="icon">📭</span><h3>No clips this day</h3></div>'; return; }
    clips.forEach(c => { allClipIds.push(c.id); grid.appendChild(buildCard(c)); });
  }).catch(e => toast('Failed to load clips', true));
}

// ── Automations page copy buttons ──────────────────────────────────────────
document.querySelectorAll('.copy-btn').forEach(btn => {
  btn.addEventListener('click', e => {
    e.stopPropagation();
    const block = document.getElementById(btn.dataset.target);
    const text = block.textContent.replace(/^Copy/, '').trim();
    navigator.clipboard.writeText(text)
      .then(() => toast('Copied to clipboard'))
      .catch(() => toast('Copy failed', true));
  });
});

// ── 2FA overlay ────────────────────────────────────────────────────────────
let _twofaState = 'disconnected';
// Seq of the in-flight submission we're waiting on a result for (0 = none).
let _pendingSeq = 0;

async function checkAuthStatus() {
  try {
    const s = await fetch(_R + '/api/auth/status').then(r => r.json());
    const prev = _twofaState;
    _twofaState = s.state || 'disconnected';
    const overlay = $('twofa-overlay');
    if (_twofaState === 'needs_2fa' || _twofaState === 'error') {
      // Keep the overlay open (or open it) so the user always sees the
      // current auth status, including failures/timeouts — previously a
      // transition to 'error' closed the overlay before any message could
      // be shown, making it look like the 2FA prompt had vanished.
      overlay.classList.add('open');
      if (_twofaState === 'needs_2fa') {
        if (prev !== 'needs_2fa') {
          $('twofa-input').value = '';
          $('twofa-msg').textContent = '';
          _pendingSeq = 0;
          setTimeout(() => $('twofa-input').focus(), 80);
        }
        // If the submission we're waiting on came back rejected, let the
        // user try again instead of leaving the form stuck on "Verifying…".
        if (_pendingSeq && s.two_fa_result_seq === _pendingSeq) {
          if (s.two_fa_result_ok === false) {
            $('twofa-msg').textContent =
              s.message || 'Incorrect verification code. Please try again.';
            $('twofa-msg').style.color = 'var(--danger)';
            const btn = $('twofa-submit');
            btn.disabled = false;
            btn.textContent = 'Verify';
            $('twofa-input').value = '';
            setTimeout(() => $('twofa-input').focus(), 80);
          }
          _pendingSeq = 0;
        }
      } else {
        // 'error' — e.g. the 2FA code wasn't submitted in time, or the
        // account setup after a successful 2FA failed. Surface the reason
        // and re-enable the form so the user can try again once the add-on
        // reconnects and (if needed) prompts for a fresh code.
        $('twofa-msg').textContent = s.message || 'Authentication failed.';
        $('twofa-msg').style.color = 'var(--danger)';
        const btn = $('twofa-submit');
        btn.disabled = false;
        btn.textContent = 'Verify';
        _pendingSeq = 0;
      }
    } else {
      if ((prev === 'needs_2fa' || prev === 'error') && _twofaState === 'connected') {
        toast('Signed in to Blink ✓');
        loadAll();
      }
      overlay.classList.remove('open');
      _pendingSeq = 0;
    }
  } catch {}
}

async function submitTwoFA() {
  const raw = $('twofa-input').value.trim().replace(/\s/g, '');
  if (!/^\d{6}$/.test(raw)) {
    $('twofa-msg').textContent = 'Please enter exactly 6 digits.';
    $('twofa-msg').style.color = 'var(--warn)';
    $('twofa-input').focus();
    return;
  }
  const btn = $('twofa-submit');
  btn.disabled = true;
  btn.textContent = '⏳ Verifying…';
  $('twofa-msg').textContent = 'Submitting code…';
  $('twofa-msg').style.color = 'var(--muted)';
  try {
    const resp = await fetch(_R + '/api/auth/2fa', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({code: raw}),
    });
    if (!resp.ok) {
      const txt = await resp.text().catch(() => 'Error');
      $('twofa-msg').textContent = txt;
      $('twofa-msg').style.color = 'var(--danger)';
      // Re-enable so the user can correct and retry.
      btn.disabled = false;
      btn.textContent = 'Verify';
    } else {
      const data = await resp.json().catch(() => ({}));
      _pendingSeq = data.seq || 0;
      // Keep button disabled — checkAuthStatus() polls every 3 s and will
      // close the overlay on success, or re-enable this button with an
      // error message if Blink rejects the code.
      btn.textContent = '✓ Submitted';
      $('twofa-msg').textContent = 'Code submitted — waiting for confirmation…';
      $('twofa-msg').style.color = 'var(--text)';
      $('twofa-input').value = '';
    }
  } catch {
    $('twofa-msg').textContent = 'Network error — could not submit code.';
    $('twofa-msg').style.color = 'var(--danger)';
    // Re-enable so the user can try again after the network recovers.
    btn.disabled = false;
    btn.textContent = 'Verify';
  }
}

$('twofa-submit').addEventListener('click', submitTwoFA);
$('twofa-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') submitTwoFA();
  // Allow only digits, backspace, delete, arrow keys, tab
  if (e.key.length === 1 && !/\d/.test(e.key)) e.preventDefault();
});

// Poll auth status every 3 seconds so the overlay appears / disappears promptly.
checkAuthStatus();
setInterval(checkAuthStatus, 3000);

// ── Boot ───────────────────────────────────────────────────────────────────
async function loadAll() {
  await Promise.all([loadStats(), loadCameras(), loadClips(0), loadAIStatus()]);
}
loadAll();
loadTagFilter();
// Auto-refresh every 60 s when Library is visible and modal is closed
// Only refresh stats/cameras to avoid replacing the clip grid while the user is browsing.
setInterval(() => {
  if (document.querySelector('[data-tab="library"]').classList.contains('active')
      && !$('modal-bg').classList.contains('open')) {
    loadStats();
    loadCameras();
  }
}, 60000);

// ── Per-clip AI analysis panel ────────────────────────────
function toggleAIPanel() {
  const hdr = $('ai-panel-hdr');
  const body = $('ai-panel-body');
  if (!hdr || !body) return;
  const isOpen = body.classList.contains('open');
  hdr.classList.toggle('open', !isOpen);
  body.classList.toggle('open', !isOpen);
  if (!isOpen && !body.dataset.loaded) {
    body.dataset.loaded = '1';
    loadClipAIResult(currentClipId);
  }
}

async function loadClipAIResult(clipId) {
  if (!clipId) return;
  const content = $('ai-panel-content');
  if (!content) return;
  content.innerHTML = '<span style="color:var(--muted);font-size:.8rem">Loading…</span>';
  try {
    const r = await api('/api/ai/results/' + clipId);
    if (!r) {
      content.innerHTML =
        '<div style="color:var(--muted);font-size:.8rem;margin-bottom:.45rem">Not analyzed yet</div>' +
        '<button class="btn sm" onclick="analyzeClipNow(\'' + _escJs(clipId) + '\')">🔬 Analyze Now</button>';
      const badge = $('ai-panel-badge');
      if (badge) { badge.textContent = ''; badge.style.color = ''; }
      return;
    }
    const conf = Math.round((r.confidence || 0) * 100);
    const isSusp = r.is_suspicious;
    _currentPromptText = r.prompt_text || '';
    const badge = $('ai-panel-badge');
    if (badge) {
      badge.textContent = isSusp ? ' ⚠' : ' ✓';
      badge.style.color = isSusp ? 'var(--danger)' : 'var(--success)';
    }
    const statusBadge = isSusp
      ? '<span class="ai-badge-suspicious">⚠ Suspicious</span>'
      : '<span class="ai-badge-clean">✓ Clear</span>';
    const confColor = isSusp ? 'var(--danger)' : 'var(--success)';
    let feedback = null;
    try { feedback = await api('/api/ai/feedback/' + clipId); } catch(e) { /* non-fatal */ }
    content.innerHTML =
      '<div class="ai-result-box">' +
        '<div style="display:flex;align-items:center;gap:.55rem;margin-bottom:.4rem">' +
          statusBadge +
          '<span style="color:' + confColor + ';font-weight:600">' + conf + '% confidence</span>' +
        '</div>' +
        (r.summary ? '<div style="color:var(--text);line-height:1.45;margin-bottom:.4rem">' + _esc(r.summary) + '</div>' : '') +
        '<div style="color:var(--muted);font-size:.74rem;margin-bottom:.4rem">' +
          'Model: ' + _esc(r.model || '—') +
          (r.analyzed_at ? ' &nbsp;·&nbsp; ' + new Date(r.analyzed_at).toLocaleString() : '') +
          (r.frame_count ? ' &nbsp;·&nbsp; ' + r.frame_count + ' frame(s) analyzed' : '') +
        '</div>' +
        '<div style="display:flex;gap:.4rem;align-items:center;flex-wrap:wrap">' +
          '<button class="btn sm ghost" onclick="analyzeClipNow(\'' + _escJs(clipId) + '\')">↺ Re-analyze</button>' +
          '<button class="btn sm ghost" id="ai-raw-toggle-btn" onclick="toggleRawResponse()">📄 Full response</button>' +
          (_promptDebugEnabled ? '<button class="btn sm ghost" onclick="showPromptModal(_currentPromptText)">📝 Prompt</button>' : '') +
        '</div>' +
        '<div id="ai-raw-response" style="display:none;margin-top:.4rem;font-size:.73rem;font-family:monospace;' +
             'background:var(--card2);border-radius:4px;padding:.4rem .5rem;white-space:pre-wrap;' +
             'color:var(--muted);max-height:120px;overflow-y:auto">' +
          _esc(r.response_text || '') +
        '</div>' +
        '<div id="ai-feedback-block" style="margin-top:.55rem;padding-top:.5rem;border-top:1px solid var(--border)">' +
          _feedbackButtonsHtml(clipId, feedback) +
        '</div>' +
      '</div>';
  } catch(e) {
    const content2 = $('ai-panel-content');
    if (content2) content2.innerHTML = '<span style="color:var(--danger);font-size:.8rem">Failed to load analysis</span>';
  }
}

// ── Adaptive learning: feedback on stored AI verdicts ──────────────────────
async function submitFeedback(clipId, correct, note, correctedSuspicious) {
  try {
    await api('/api/ai/feedback/' + clipId, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        correct: correct,
        correction_note: note || '',
        corrected_suspicious: correctedSuspicious === undefined ? null : correctedSuspicious,
      }),
    });
    toast('Feedback recorded — thanks!');
    return true;
  } catch(e) {
    toast('Failed to save feedback', true);
    return false;
  }
}

function _feedbackButtonsHtml(clipId, fb) {
  if (fb) {
    const verdict = fb.correct
      ? '<span style="color:var(--success)">👍 Marked correct</span>'
      : '<span style="color:var(--warn)">👎 Marked incorrect' +
        (fb.correction_note ? ' — "' + _esc(fb.correction_note) + '"' : '') + '</span>';
    return '<div style="font-size:.78rem;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">' +
        verdict +
        '<button class="btn sm ghost" onclick="_resetFeedbackForm(\'' + _escJs(clipId) + '\')">Change</button>' +
      '</div><div id="ai-feedback-form"></div>';
  }
  return '<div style="font-size:.78rem;display:flex;align-items:center;gap:.5rem;flex-wrap:wrap">' +
      '<span style="color:var(--muted)">Was this verdict correct?</span>' +
      '<button class="btn sm ghost" onclick="_quickFeedback(\'' + _escJs(clipId) + '\', true)">👍 Correct</button>' +
      '<button class="btn sm ghost" onclick="_showFeedbackNoteForm(\'' + _escJs(clipId) + '\')">👎 Incorrect</button>' +
    '</div><div id="ai-feedback-form"></div>';
}

function _resetFeedbackForm(clipId) {
  const block = $('ai-feedback-block');
  if (block) block.innerHTML = _feedbackButtonsHtml(clipId, null);
}

async function _quickFeedback(clipId, correct) {
  const ok = await submitFeedback(clipId, correct);
  if (ok) await loadClipAIResult(clipId);
}

function _showFeedbackNoteForm(clipId) {
  const form = $('ai-feedback-form');
  if (!form) return;
  form.innerHTML =
    '<div style="margin-top:.4rem;display:flex;flex-direction:column;gap:.35rem">' +
      '<input class="tag-input" style="width:100%" id="feedback-note-input" placeholder="What actually happened? (optional)">' +
      '<label style="font-size:.75rem;color:var(--muted);display:flex;align-items:center;gap:.3rem">' +
        '<input type="checkbox" id="feedback-corrected-suspicious"> Should have been flagged suspicious instead' +
      '</label>' +
      '<div style="display:flex;gap:.4rem">' +
        '<button class="btn sm" onclick="_submitFeedbackForm(\'' + _escJs(clipId) + '\')">Submit</button>' +
        '<button class="btn sm ghost" onclick="$(\'ai-feedback-form\').innerHTML=\'\'">Cancel</button>' +
      '</div>' +
    '</div>';
}

async function _submitFeedbackForm(clipId) {
  const noteInput = $('feedback-note-input');
  const note = noteInput ? noteInput.value : '';
  const correctedCheckbox = $('feedback-corrected-suspicious');
  const correctedSuspicious = correctedCheckbox && correctedCheckbox.checked ? true : undefined;
  const ok = await submitFeedback(clipId, false, note, correctedSuspicious);
  if (ok) await loadClipAIResult(clipId);
}

function toggleRawResponse() {
  const el = $('ai-raw-response');
  const btn = $('ai-raw-toggle-btn');
  if (!el) return;
  const hidden = el.style.display === 'none';
  el.style.display = hidden ? 'block' : 'none';
  if (btn) btn.textContent = hidden ? '📄 Hide response' : '📄 Full response';
}

async function analyzeClipNow(clipId) {
  const content = $('ai-panel-content');
  if (content) content.innerHTML = '<span style="color:var(--muted);font-size:.8rem">⏳ Analyzing… (may take 30–120 s)</span>';
  try {
    await api('/api/ai/analyze/' + clipId, { method: 'POST' });
    const body = $('ai-panel-body');
    if (body) delete body.dataset.loaded;
    await loadClipAIResult(clipId);
    toast('AI analysis complete');
  } catch(e) {
    if (content) content.innerHTML = '<span style="color:var(--danger);font-size:.8rem">Analysis failed — check AI connection on the AI tab</span>';
    toast('Analysis failed', true);
  }
}

// ── AI tab: test analysis ──────────────────────────────────
async function runAITest() {
  const btn = $('ai-test-btn');
  const resultEl = $('ai-test-result');
  if (!btn || !resultEl) return;
  btn.disabled = true;
  btn.textContent = '⏳ Testing…';
  resultEl.style.display = 'block';
  resultEl.innerHTML = '<span style="color:var(--muted);font-size:.8rem">Fetching a recent clip…</span>';
  try {
    const clips = await api('/api/clips?limit=1&sort=newest');
    if (!clips || !clips.length) {
      resultEl.innerHTML = '<span style="color:var(--warn);font-size:.8rem">No clips in the library yet. Download a clip first, then run the test.</span>';
      return;
    }
    const clip = clips[0];
    resultEl.innerHTML = '<span style="color:var(--muted);font-size:.8rem">Analyzing ' + _esc(clip.camera) + ' clip… (may take a minute)</span>';
    const r = await api('/api/ai/analyze/' + clip.id, { method: 'POST' });
    const conf = Math.round((r.confidence || 0) * 100);
    const resultColor = r.is_suspicious ? 'var(--danger)' : 'var(--success)';
    const resultLabel = r.is_suspicious ? '⚠ Suspicious' : '✓ Clear';
    resultEl.innerHTML =
      '<div style="background:var(--card2);border:1px solid var(--border);border-radius:var(--radius);' +
           'padding:.6rem .8rem;font-size:.82rem;margin-top:.3rem">' +
        '<div style="color:var(--success);font-weight:700;margin-bottom:.35rem">✓ AI is working!</div>' +
        '<div>Camera: <strong>' + _esc(r.camera || clip.camera) + '</strong></div>' +
        '<div>Result: <span style="color:' + resultColor + ';font-weight:600">' + resultLabel + '</span> (' + conf + '% confidence)</div>' +
        (r.summary ? '<div style="color:var(--muted);margin-top:.3rem">' + _esc(r.summary) + '</div>' : '') +
        '<div style="color:var(--muted);font-size:.74rem;margin-top:.3rem">' +
          'Model: ' + _esc(r.model || '—') +
          ' &nbsp;·&nbsp; ' + (r.frame_count || 0) + ' frame(s)' +
          ' &nbsp;·&nbsp; ' + ((r.analysis_duration || 0)).toFixed(1) + 's' +
        '</div>' +
      '</div>';
    toast('Test complete — AI is working ✓');
  } catch(e) {
    resultEl.innerHTML = '<span style="color:var(--danger);font-size:.8rem">Test failed — check AI provider settings and connection</span>';
    toast('AI test failed', true);
  } finally {
    btn.disabled = false;
    btn.textContent = '🔬 Test Analysis';
  }
}

// ── AI tab: send test email ────────────────────────────────
async function runTestEmail() {
  const btn = $('ai-test-email-btn');
  const resultEl = $('ai-test-email-result');
  if (!btn || !resultEl) return;
  btn.disabled = true;
  btn.textContent = '⏳ Sending…';
  resultEl.style.display = 'block';
  resultEl.style.color = 'var(--muted)';
  resultEl.textContent = 'Sending test email…';
  try {
    const r = await api('/api/notifications/test-email', { method: 'POST' });
    resultEl.style.color = r.success ? 'var(--success)' : 'var(--danger)';
    resultEl.textContent = (r.success ? '✓ ' : '✗ ') + r.message;
    toast(r.success ? 'Test email sent' : 'Test email failed', !r.success);
  } catch(e) {
    resultEl.style.color = 'var(--danger)';
    resultEl.textContent = '✗ Failed to send test email — check the add-on logs';
    toast('Test email failed', true);
  } finally {
    btn.disabled = false;
    btn.textContent = '✉️ Send Test Email';
  }
}

// ── AI Analysis Tab ──────────────────────────────────────
let _aiEnabled = false;
let _promptDebugEnabled = false;
let _currentPromptText = '';
let _carProtectionActive = null;
function _updateCarProtectionWarning() {
  const el = $('ai-car-protection-warning');
  if (!el) return;
  const anyCarCamera = (_camConfigsData || []).some(c => c.is_car_camera);
  el.style.display = (anyCarCamera && _carProtectionActive === false) ? 'block' : 'none';
}
async function loadAIStatus() {
  try {
    const d = await api('/api/ai/status');
    _aiEnabled = d.enabled;
    _promptDebugEnabled = !!d.prompt_debug_enabled;
    _carProtectionActive = 'car_protection_active' in d ? d.car_protection_active : null;
    _updateCarProtectionWarning();
    $('ai-smtp-configured').style.display = d.smtp_configured ? 'block' : 'none';
    $('ai-smtp-not-configured').style.display = d.smtp_configured ? 'none' : 'block';
    $('ai-disabled-msg').style.display = d.enabled ? 'none' : 'block';
    $('ai-content').style.display = d.enabled ? 'block' : 'none';
    if (!d.enabled) return;
    const badge = $('ai-status-badge');
    const providerLabels = {
      ollama: 'Ollama (Local/LAN)',
      ollama_cloud: 'Ollama Cloud',
      moondream_cloud: 'Moondream Cloud',
      moondream_local: 'Moondream Local (0.5B)',
      anthropic: 'Anthropic (Claude)',
      openai: 'OpenAI (GPT)',
    };
    const provider = d.provider || 'ollama';
    if (d.ai_online) {
      badge.style.color = 'var(--success)';
      $('ai-status-text').textContent = 'Connected';
    } else {
      badge.style.color = 'var(--danger)';
      $('ai-status-text').textContent = 'Offline';
    }
    $('ai-provider-label').textContent = providerLabels[provider] || provider;
    $('ai-model-name').textContent = d.model || '—';
    const modelPicker = $('ai-model-picker');
    const showPicker = provider === 'ollama' || provider === 'ollama_cloud' || provider === 'anthropic' || provider === 'openai';
    if (modelPicker) modelPicker.style.display = showPicker ? 'flex' : 'none';
    // Moondream local: show install section and poll install state
    const mdSection = $('ai-moondream-local-section');
    if (mdSection) mdSection.style.display = provider === 'moondream_local' ? 'block' : 'none';
    if (provider === 'moondream_local') {
      _moondreamArchSupported = d.moondream_arch_supported !== false;
      _updateMoondreamInstallUI(d.moondream_installed);
    }
    const escInfo = $('ai-escalation-info');
    if (escInfo) {
      if (d.escalation_provider) {
        escInfo.style.display = 'block';
        const escLabel = (providerLabels[d.escalation_provider] || d.escalation_provider) + ' — ' + (d.escalation_model || '—');
        $('ai-escalation-label').textContent = escLabel;
        const escStatus = $('ai-escalation-status');
        if (escStatus) {
          escStatus.textContent = d.escalation_online ? ' 🟢 online' : ' 🔴 unreachable — falling back to tier 1';
          escStatus.style.color = d.escalation_online ? 'var(--success)' : 'var(--danger)';
        }
      } else {
        escInfo.style.display = 'none';
      }
    }
    const finetuneCard = $('ai-finetune-card');
    if (finetuneCard) {
      finetuneCard.style.display = provider === 'moondream_cloud' ? 'block' : 'none';
      if (provider === 'moondream_cloud') loadFinetunePanel();
    }
    loadAdaptiveLearning();
    if (d.queue) {
      $('ai-q-pending').textContent = d.queue.pending || 0;
      $('ai-q-processing').textContent = d.queue.processing || 0;
      $('ai-q-completed').textContent = d.queue.completed || 0;
      $('ai-q-failed').textContent = d.queue.failed || 0;
      const si = $('ai-schedule-info');
      if (d.queue.schedule_start && d.queue.schedule_end) {
        const st = d.queue.in_schedule ? '🟢 Active' : '🔴 Waiting';
        si.innerHTML = d.queue.schedule_start + ' – ' + d.queue.schedule_end + '<br>' + st;
      } else {
        si.textContent = 'Always active (no schedule set)';
      }
    }
    if (d.analysis_stats) {
      $('ai-stat-total').textContent = d.analysis_stats.total_analyzed || 0;
      $('ai-stat-suspicious').textContent = d.analysis_stats.suspicious_count || 0;
      $('ai-stat-last').textContent = d.analysis_stats.last_analysis
        ? new Date(d.analysis_stats.last_analysis).toLocaleString() : '—';
    }
  } catch(e) { console.error('AI status error', e); }
}

async function loadSuspiciousFeed() {
  try {
    const items = await api('/api/ai/suspicious?limit=20');
    const el = $('ai-suspicious-feed');
    if (!items || items.length === 0) {
      el.innerHTML = '<div style="color:var(--muted);padding:1rem;text-align:center">No suspicious activity detected yet.</div>';
      return;
    }
    el.innerHTML = items.map(r => {
      const dt = new Date(r.analyzed_at).toLocaleString();
      const conf = Math.round((r.confidence||0)*100);
      const confColor = conf > 70 ? 'var(--danger)' : 'var(--warn)';
      return '<div class="card" style="padding:.8rem;display:flex;align-items:center;gap:1rem;cursor:pointer" onclick="openModal(\''+_escJs(r.clip_id)+'\')">'+
        '<div style="font-size:1.3rem">⚠️</div>'+
        '<div style="flex:1;min-width:0">'+
          '<div style="font-weight:600;font-size:.85rem">'+_esc(r.camera)+'</div>'+
          '<div style="font-size:.78rem;color:var(--muted)">'+dt+'</div>'+
          '<div style="font-size:.82rem;margin-top:.3rem">'+_esc(r.summary||'')+'</div>'+
        '</div>'+
        '<div style="text-align:center;min-width:50px">'+
          '<div style="font-size:1.1rem;font-weight:700;color:'+confColor+'">'+conf+'%</div>'+
          '<div style="font-size:.65rem;color:var(--muted)">confidence</div>'+
        '</div>'+
        '<div style="display:flex;gap:.25rem" onclick="event.stopPropagation()">'+
          '<button class="btn sm ghost" title="Correct" onclick="_feedQuickFeedback(\''+_escJs(r.clip_id)+'\', true, this)">👍</button>'+
          '<button class="btn sm ghost" title="Incorrect" onclick="_feedQuickFeedback(\''+_escJs(r.clip_id)+'\', false, this)">👎</button>'+
        '</div>'+
      '</div>';
    }).join('');
  } catch(e) { console.error('Suspicious feed error', e); }
}

async function _feedQuickFeedback(clipId, correct, btnEl) {
  const ok = await submitFeedback(clipId, correct);
  if (ok && btnEl && btnEl.parentElement) {
    btnEl.parentElement.innerHTML = '<span style="font-size:.72rem;color:var(--muted)">Thanks!</span>';
  }
}

// ── Adaptive Learning card ──────────────────────────────────────────────
async function loadAdaptiveLearning() {
  try {
    const stats = await api('/api/ai/feedback/stats');
    const totalEl = $('ai-fb-total');
    const accuracyEl = $('ai-fb-accuracy');
    const breakdownEl = $('ai-fb-breakdown');
    if (!totalEl || !accuracyEl || !breakdownEl) return;
    totalEl.textContent = stats.total || 0;
    if (stats.total > 0) {
      const pct = Math.round((stats.correct / stats.total) * 100);
      accuracyEl.textContent = pct + '%';
      accuracyEl.style.color = pct >= 80 ? 'var(--success)' : pct >= 50 ? 'var(--warn)' : 'var(--danger)';
      breakdownEl.textContent = (stats.false_positive || 0) + ' false positive(s), ' +
        (stats.false_negative || 0) + ' false negative(s) reported';
    } else {
      accuracyEl.textContent = '—';
      accuracyEl.style.color = 'var(--muted)';
      breakdownEl.textContent = 'No feedback recorded yet.';
    }
  } catch(e) { console.error('Adaptive learning stats error', e); }
}

// ── Moondream Fine-Tuning card ──────────────────────────────────────────
async function loadFinetunePanel() {
  const listEl = $('ai-finetune-list');
  if (!listEl) return;
  listEl.innerHTML = '<div style="color:var(--muted);font-size:.8rem">Loading…</div>';
  try {
    const d = await api('/api/ai/finetune');
    if (!d.enabled || !d.finetunes || d.finetunes.length === 0) {
      listEl.innerHTML = '<div style="color:var(--muted);font-size:.8rem">No fine-tunes yet — create one below.</div>';
      return;
    }
    listEl.innerHTML = d.finetunes.map(ft => {
      const id = _escJs(ft.finetune_id || ft.id || '');
      return '<div style="display:flex;align-items:center;justify-content:space-between;gap:.5rem;padding:.4rem .55rem;background:var(--card2);border-radius:var(--radius)">' +
        '<span style="font-size:.82rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _esc(ft.name || ft.finetune_id || '—') + '</span>' +
        '<div style="display:flex;gap:.3rem;flex-shrink:0">' +
          '<button class="btn sm ghost" onclick="_viewFinetuneCheckpoints(\'' + id + '\')">Checkpoints</button>' +
          '<button class="btn sm danger" onclick="_deleteFinetune(\'' + id + '\')">🗑</button>' +
        '</div>' +
      '</div>';
    }).join('');
  } catch(e) {
    listEl.innerHTML = '<div style="color:var(--danger);font-size:.8rem">Failed to load fine-tunes</div>';
  }
}

async function _createFinetune() {
  const nameInput = $('ai-finetune-name');
  const rankSelect = $('ai-finetune-rank');
  const name = nameInput ? nameInput.value.trim() : '';
  if (!name) { toast('Enter a name for the fine-tune', true); return; }
  const rank = rankSelect ? parseInt(rankSelect.value, 10) : 16;
  try {
    await api('/api/ai/finetune', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, rank: rank }),
    });
    toast('Fine-tune created');
    if (nameInput) nameInput.value = '';
    await loadFinetunePanel();
  } catch(e) {
    toast('Failed to create fine-tune', true);
  }
}

async function _deleteFinetune(finetuneId) {
  const ok = await showConfirmModal(
    'Delete this fine-tune and all its checkpoints? This cannot be undone.',
    'Delete fine-tune?'
  );
  if (!ok) return;
  try {
    await api('/api/ai/finetune/' + finetuneId, { method: 'DELETE' });
    toast('Fine-tune deleted');
    await loadFinetunePanel();
  } catch(e) {
    toast('Failed to delete fine-tune', true);
  }
}

async function _viewFinetuneCheckpoints(finetuneId) {
  const listEl = $('ai-finetune-list');
  if (!listEl) return;
  try {
    const d = await api('/api/ai/finetune/' + finetuneId + '/checkpoints');
    const checkpoints = d.checkpoints || [];
    if (checkpoints.length === 0) {
      toast('No checkpoints saved yet for this fine-tune');
      return;
    }
    listEl.innerHTML = checkpoints.map(cp => {
      return '<div style="display:flex;align-items:center;justify-content:space-between;gap:.5rem;padding:.4rem .55rem;background:var(--card2);border-radius:var(--radius)">' +
        '<span style="font-size:.82rem">Step ' + _esc(String(cp.step)) + '</span>' +
        '<button class="btn sm" onclick="_activateFinetune(\'' + _escJs(finetuneId) + '\', ' + JSON.stringify(cp.step) + ')">Activate</button>' +
      '</div>';
    }).join('') + '<button class="btn sm ghost" style="margin-top:.4rem" onclick="loadFinetunePanel()">← Back to fine-tunes</button>';
  } catch(e) {
    toast('Failed to load checkpoints', true);
  }
}

async function _activateFinetune(finetuneId, step) {
  try {
    const r = await api('/api/ai/finetune/' + finetuneId + '/activate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ step: step }),
    });
    toast('Activated: ' + r.model);
    await loadAIStatus();
  } catch(e) {
    toast('Failed to activate checkpoint', true);
  }
}

// Safe for both HTML text content AND quoted attribute values: the
// textContent round-trip escapes &, <, > but leaves quote characters raw,
// which is exploitable wherever the result is interpolated into
// data-foo="${_esc(x)}" (an untrusted value containing a bare " would
// otherwise break out of the attribute and inject arbitrary markup).
function _esc(s) {
  const d = document.createElement('div');
  d.textContent = s;
  return d.innerHTML.replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

// For interpolating into a single-quoted JS string literal that itself sits
// inside an HTML attribute (e.g. onclick="fn('${_escJs(x)}')"). The browser
// HTML-decodes the attribute value before handing it to the JS parser, so a
// bare _esc() quote-escape (-> &#39;) round-trips back to a literal ' and
// would still let the string break out early — escaping the backslash/quote
// for the JS layer first, then _esc() for the HTML layer, survives both.
function _escJs(s) {
  return _esc(String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'"));
}

// ── Camera Configurations ──────────────────────────────────────────────────
let _camConfigsData = [];

async function loadCameraConfigs() {
  const listEl = $('ai-cam-configs-list');
  const loadingEl = $('ai-cam-configs-loading');
  if (!listEl || !loadingEl) return;
  loadingEl.style.display = 'block';
  listEl.innerHTML = '';
  try {
    _camConfigsData = await api('/api/ai/camera-configs');
    loadingEl.style.display = 'none';
    _updateCarProtectionWarning();
    if (!_camConfigsData.length) {
      listEl.innerHTML = '<div style="color:var(--muted);font-size:.84rem;padding:.5rem 0">No cameras found. Download at least one clip to populate the camera list.</div>';
      return;
    }
    listEl.innerHTML = _camConfigsData.map((c, i) =>
      `<div class="status-card" style="padding:.85rem 1rem">
        <div style="display:flex;align-items:center;gap:.6rem;margin-bottom:.55rem">
          <span style="font-weight:600;font-size:.88rem;color:var(--accent)">📷 ${_esc(c.camera)}</span>
        </div>
        <div style="margin-bottom:.45rem">
          <label style="font-size:.76rem;color:var(--muted);display:block;margin-bottom:.2rem">Camera purpose / description</label>
          <input type="text" class="tag-input" style="width:100%" data-cam="${_esc(c.camera)}" data-field="description"
            placeholder="e.g. Points at driveway, monitors the silver Kia Forte. Watch for anyone approaching the car."
            value="${_esc(c.description || '')}">
        </div>
        <div style="margin-bottom:.45rem">
          <label style="font-size:.76rem;color:var(--muted);display:block;margin-bottom:.2rem">Custom AI prompt (overrides global prompt for this camera — optional)</label>
          <input type="text" class="tag-input" style="width:100%" data-cam="${_esc(c.camera)}" data-field="custom_prompt"
            placeholder="Leave empty to use the global AI prompt"
            value="${_esc(c.custom_prompt || '')}">
        </div>
        <div style="display:flex;align-items:center;gap:.5rem;padding:.4rem .55rem;background:var(--bg2,rgba(255,255,255,.04));border-radius:.4rem;border:1px solid var(--border,rgba(255,255,255,.1))">
          <input type="checkbox" id="cam-car-chk-${i}" data-cam="${_esc(c.camera)}" data-field="is_car_camera"
            ${c.is_car_camera ? 'checked' : ''} style="cursor:pointer;width:1rem;height:1rem;accent-color:var(--accent,#5b9cf6)"
            onchange="document.getElementById('cam-zone-${i}').style.display = this.checked ? 'block' : 'none'">
          <label for="cam-car-chk-${i}" style="font-size:.76rem;color:var(--fg,#e2e8f0);cursor:pointer;line-height:1.3">
            <strong>Protected vehicle visible from this camera</strong> — enables car-proximity alert rules
          </label>
        </div>
        <div id="cam-zone-${i}" style="display:${c.is_car_camera ? 'block' : 'none'};margin-top:.45rem;padding:.5rem .55rem;background:var(--bg2,rgba(255,255,255,.04));border-radius:.4rem;border:1px solid var(--border,rgba(255,255,255,.1))">
          <label style="font-size:.76rem;color:var(--muted);display:block;margin-bottom:.35rem">
            Car zone (optional) — roughly where the vehicle normally sits, as % of the frame (0 = left/top edge, 100 = right/bottom edge). Sharpens accuracy when detection is ambiguous. Leave blank to skip.
          </label>
          <div style="display:grid;grid-template-columns:repeat(4,minmax(min(70px,100%),1fr));gap:.4rem">
            <input type="number" class="tag-input" placeholder="Left %" min="0" max="100" step="1"
              data-cam="${_esc(c.camera)}" data-zone-field="x_min"
              value="${c.car_zone ? Math.round(c.car_zone.x_min * 100) : ''}">
            <input type="number" class="tag-input" placeholder="Top %" min="0" max="100" step="1"
              data-cam="${_esc(c.camera)}" data-zone-field="y_min"
              value="${c.car_zone ? Math.round(c.car_zone.y_min * 100) : ''}">
            <input type="number" class="tag-input" placeholder="Right %" min="0" max="100" step="1"
              data-cam="${_esc(c.camera)}" data-zone-field="x_max"
              value="${c.car_zone ? Math.round(c.car_zone.x_max * 100) : ''}">
            <input type="number" class="tag-input" placeholder="Bottom %" min="0" max="100" step="1"
              data-cam="${_esc(c.camera)}" data-zone-field="y_max"
              value="${c.car_zone ? Math.round(c.car_zone.y_max * 100) : ''}">
          </div>
        </div>
      </div>`
    ).join('');
  } catch(e) {
    loadingEl.style.display = 'none';
    listEl.innerHTML = '<div style="color:var(--danger);font-size:.84rem">Failed to load camera configs.</div>';
  }
}

async function saveCameraConfigs() {
  const btn = $('ai-cam-save-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ Saving…'; }
  try {
    const byCamera = {};
    // Text/number inputs
    document.querySelectorAll('#ai-cam-configs-list input[type=text][data-cam]').forEach(inp => {
      const cam = inp.dataset.cam;
      const field = inp.dataset.field;
      if (!byCamera[cam]) byCamera[cam] = { camera: cam, description: '', custom_prompt: '', is_car_camera: false };
      byCamera[cam][field] = inp.value.trim();
    });
    // Checkboxes (is_car_camera)
    document.querySelectorAll('#ai-cam-configs-list input[type=checkbox][data-cam]').forEach(chk => {
      const cam = chk.dataset.cam;
      if (!byCamera[cam]) byCamera[cam] = { camera: cam, description: '', custom_prompt: '', is_car_camera: false };
      byCamera[cam][chk.dataset.field] = chk.checked;
    });
    // Car zone (4 % inputs -> normalised 0-1 rectangle, or null if incomplete)
    const zoneRaw = {};
    document.querySelectorAll('#ai-cam-configs-list input[type=number][data-zone-field]').forEach(inp => {
      const cam = inp.dataset.cam;
      if (!zoneRaw[cam]) zoneRaw[cam] = {};
      const v = inp.value.trim();
      if (v !== '') zoneRaw[cam][inp.dataset.zoneField] = parseFloat(v) / 100;
    });
    Object.keys(byCamera).forEach(cam => {
      const z = zoneRaw[cam];
      const complete = z && ['x_min', 'y_min', 'x_max', 'y_max'].every(
        k => typeof z[k] === 'number' && !isNaN(z[k])
      );
      byCamera[cam].car_zone = complete ? z : null;
    });
    const payload = Object.values(byCamera);
    await api('/api/ai/camera-configs', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    _camConfigsData = payload;
    _updateCarProtectionWarning();
    toast('Camera configs saved ✓');
  } catch(e) { toast('Failed to save camera configs', true); }
  finally {
    if (btn) { btn.disabled = false; btn.textContent = '💾 Save Camera Configs'; }
  }
}

const camSaveBtn = $('ai-cam-save-btn');
if (camSaveBtn) camSaveBtn.addEventListener('click', saveCameraConfigs);

$('ai-fetch-models-btn').addEventListener('click', async () => {
  const btn = $('ai-fetch-models-btn');
  btn.disabled = true; btn.textContent = '⏳ Loading…';
  try {
    const d = await api('/api/ai/models');
    const models = d.models || [];
    const sel = $('ai-model-select');
    sel.innerHTML = '<option value="">Select a model…</option>';
    models.forEach((m, i) => {
      const o = document.createElement('option');
      o.value = m.name;
      const gb = m.size ? ' · ' + (m.size / 1e9).toFixed(1) + ' GB' : '';
      const star = i === 0 ? ' ⭐ Best' : '';
      o.textContent = m.name + gb + star;
      sel.appendChild(o);
    });
    // Auto-select the top-ranked model
    if (models.length && !sel.value) sel.value = models[0].name;
    const count = models.length;
    toast(count ? 'Found ' + count + ' vision model(s) — best shown first' : 'No vision models found on this Ollama server');
  } catch(e) { toast('Failed to fetch models', true); }
  btn.disabled = false; btn.textContent = '⟳ Fetch Models';
});

$('ai-copy-model-btn').addEventListener('click', () => {
  const modelId = $('ai-model-select').value;
  if (!modelId) { toast('Fetch models and pick one first', true); return; }
  navigator.clipboard.writeText(modelId)
    .then(() => toast('Copied "' + modelId + '" — paste into the add-on Configuration tab'))
    .catch(() => toast(modelId, true));
});

// ── Moondream local install ───────────────────────────────
let _moondreamArchSupported = true;

function _updateMoondreamInstallUI(installed) {
  const archUnsup = $('ai-md-arch-unsupported');
  const notInst = $('ai-md-not-installed');
  const installing = $('ai-md-installing');
  const instd = $('ai-md-installed');
  const failed = $('ai-md-failed');
  [archUnsup, notInst, installing, instd, failed].forEach(el => { if (el) el.style.display = 'none'; });

  if (!_moondreamArchSupported) {
    if (archUnsup) archUnsup.style.display = 'block';
    return;
  }

  const state = _moondreamInstallState;
  if (installed || state.status === 'installed') {
    if (instd) instd.style.display = 'block';
  } else if (state.status === 'installing') {
    if (installing) {
      installing.style.display = 'block';
      const log = $('ai-md-install-log');
      if (log) { log.textContent = state.log || ''; log.scrollTop = log.scrollHeight; }
    }
    // Poll for completion
    setTimeout(_pollMoondreamInstallStatus, 2500);
  } else if (state.status === 'failed') {
    if (failed) {
      failed.style.display = 'block';
      const log = $('ai-md-fail-log');
      if (log) log.textContent = state.log || '';
    }
  } else {
    if (notInst) notInst.style.display = 'block';
  }
}

let _moondreamInstallState = { status: 'idle', log: '' };

async function _pollMoondreamInstallStatus() {
  try {
    const s = await api('/api/ai/moondream/install-status');
    _moondreamArchSupported = s.arch_supported !== false;
    _moondreamInstallState = s.install_state || { status: 'idle', log: '' };
    _updateMoondreamInstallUI(s.installed);
  } catch(e) { console.error('moondream install status error', e); }
}

async function _startMoondreamInstall() {
  try {
    _moondreamInstallState = { status: 'installing', log: 'Starting pip install moondream…\n' };
    _updateMoondreamInstallUI(false);
    await api('/api/ai/moondream/install', { method: 'POST' });
    await _pollMoondreamInstallStatus();
  } catch(e) { toast('Failed to start installation', true); }
}

$('ai-install-moondream-btn').addEventListener('click', _startMoondreamInstall);
$('ai-retry-moondream-btn').addEventListener('click', _startMoondreamInstall);
$('ai-panel-hdr').addEventListener('click', () => toggleAIPanel());
$('ai-test-btn').addEventListener('click', () => runAITest());
$('ai-test-email-btn').addEventListener('click', () => runTestEmail());
$('ai-finetune-create-btn').addEventListener('click', () => _createFinetune());

// Load AI tab when selected
document.querySelectorAll('.nav-tab').forEach(t => {
  t.addEventListener('click', () => {
    if (t.dataset.tab === 'ai') { loadAIStatus(); loadSuspiciousFeed(); loadCameraConfigs(); }
    if (t.dataset.tab === 'usage') { loadAIUsage(); }
  });
});

// Auto-refresh AI tab every 10s when visible
setInterval(() => {
  if (document.querySelector('[data-tab="ai"]') &&
      document.querySelector('[data-tab="ai"]').classList.contains('active')) {
    loadAIStatus();
  }
  if (document.querySelector('[data-tab="usage"]') &&
      document.querySelector('[data-tab="usage"]').classList.contains('active')) {
    loadAIUsage();
  }
}, 10000);

// ── AI Usage Tab ─────────────────────────────────────────
function _fmtNum(n) {
  if (n == null || n === 0) return '0';
  if (n >= 1000000) return (n / 1000000).toFixed(2) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'K';
  return String(n);
}

const _PROVIDER_NOTES = {
  ollama: 'Ollama (Local/LAN) runs on your own hardware or another device on your network — no cloud costs. Token counts are extracted from the Ollama API response (<code>prompt_eval_count</code> / <code>eval_count</code>). Some cached responses may show 0 prompt tokens.',
  ollama_cloud: 'Ollama Cloud (api.ollama.com) is a hosted Ollama service. Token counts are extracted from the API response. API usage may incur costs — check your Ollama Cloud account dashboard.',
  moondream_cloud: 'Moondream Cloud bills per API request. Each frame is analysed individually with reasoning mode enabled for better spatial accuracy. Token counts shown are <em>estimates</em> (256 image tokens + text tokens per frame) — the Moondream API does not return usage stats. Also supports fine-tuning — see the Fine-Tuning card above. Check <a href="https://moondream.ai" target="_blank" rel="noopener">moondream.ai</a> for authoritative billing.',
  moondream_local: 'Moondream Local runs entirely on-device — no cloud costs and no token tracking. The analysis count shows how many clips have been processed.',
  anthropic: 'Anthropic (Claude) charges per token. Input and output tokens are tracked for every analysis. Use <strong>Claude Haiku 4.5</strong> for best cost efficiency ($1/$5 per 1M tokens). Estimated cost is calculated from your token usage and the model\'s current pricing.',
  openai: 'OpenAI charges per token. Input and output tokens are tracked from the API response for every analysis.',
};
const _ESCALATION_NOTE = 'Two-tier escalation (<code>ai_escalation_provider</code> / <code>ai_escalation_model</code>) works with any provider as tier 2, including a different one than tier 1 — e.g. a fast OpenAI model escalating to Moondream Cloud or Claude for a closer second look. When configured, tier-1 and escalation tokens/cost are tracked and priced separately (see the escalation row in the table below).';

function _fmtCost(cost) {
  if (cost == null) return 'N/A';
  return cost < 0.001 ? '<$0.001' : '$' + cost.toFixed(4);
}

async function loadAIUsage() {
  try {
    const d = await api('/api/ai/usage');

    const totalAnalyses = d.total_analyses || 0;
    const totalTokens = d.total_tokens || 0;
    const promptTokens = d.total_tokens_prompt || 0;
    const completionTokens = d.total_tokens_completion || 0;
    const totalEscalations = d.total_escalations || 0;
    const byModel = d.by_model || [];

    $('usage-total-analyses').textContent = _fmtNum(totalAnalyses);
    $('usage-total-tokens').textContent = _fmtNum(totalTokens);
    $('usage-prompt-tokens').textContent = _fmtNum(promptTokens);
    $('usage-completion-tokens').textContent = _fmtNum(completionTokens);

    const provider = d.provider || '';
    const provLabels = {ollama:'Ollama (Local/LAN)',ollama_cloud:'Ollama Cloud',moondream_cloud:'Moondream Cloud',moondream_local:'Moondream Local (0.5B)',anthropic:'Anthropic (Claude)',openai:'OpenAI (GPT)'};
    $('usage-provider-name').textContent = provLabels[provider] || (provider || '—');
    $('usage-model-name').textContent = d.model || '—';

    const noteEl = $('usage-provider-note');
    let providerNote = _PROVIDER_NOTES[provider] || '';
    if (totalEscalations > 0) providerNote += (providerNote ? ' ' : '') + _ESCALATION_NOTE;
    if (providerNote) {
      noteEl.innerHTML = providerNote;
      noteEl.style.display = 'block';
    } else {
      noteEl.style.display = 'none';
    }

    // Providers that show token counts (real or estimated)
    const showTokens = provider === 'ollama' || provider === 'ollama_cloud'
      || provider === 'anthropic' || provider === 'openai' || provider === 'moondream_cloud';

    // Escalation may be to any provider as tier 2 (see ai_escalation_provider) —
    // these stats reflect whatever the by_model breakdown tagged "escalated".
    const escStatEl = $('usage-escalations-stat');
    const escTokensStatEl = $('usage-escalation-tokens-stat');
    if (totalEscalations > 0) {
      escStatEl.style.display = '';
      $('usage-escalations-value').textContent = _fmtNum(totalEscalations);
      escTokensStatEl.style.display = '';
      $('usage-escalation-tokens-value').textContent = _fmtNum(d.total_escalation_tokens || 0);
    } else {
      escStatEl.style.display = 'none';
      escTokensStatEl.style.display = 'none';
    }

    // Estimated cost: computed server-side per model row (each priced with
    // its own rate, not a blanket "current model" rate), so it stays
    // accurate across model switches and OpenAI escalation.
    const costStatEl = $('usage-cost-stat');
    if (d.total_estimated_cost != null && totalTokens > 0) {
      costStatEl.style.display = '';
      $('usage-cost-value').textContent = _fmtCost(d.total_estimated_cost);
    } else {
      costStatEl.style.display = 'none';
    }
    document.getElementById('usage-total-tokens').closest('.usage-stat').style.display = showTokens ? '' : 'none';
    document.getElementById('usage-prompt-tokens').closest('.usage-stat').style.display = showTokens ? '' : 'none';
    document.getElementById('usage-completion-tokens').closest('.usage-stat').style.display = showTokens ? '' : 'none';

    // Populate model breakdown table
    const tbody = $('usage-model-tbody');
    const noData = $('usage-no-data');
    if (byModel.length === 0) {
      tbody.innerHTML = '';
      noData.style.display = '';
    } else {
      noData.style.display = 'none';
      tbody.innerHTML = byModel.map(m => {
        const tp = parseInt(m.tokens_prompt || 0);
        const tc = parseInt(m.tokens_completion || 0);
        const tt = tp + tc;
        const tokensHtml = showTokens
          ? `<td style="text-align:right">${_fmtNum(tp)}</td><td style="text-align:right">${_fmtNum(tc)}</td><td style="text-align:right">${_fmtNum(tt)}</td>`
          : `<td style="text-align:right;color:var(--muted)">N/A</td><td style="text-align:right;color:var(--muted)">N/A</td><td style="text-align:right;color:var(--muted)">N/A</td>`;
        const costHtml = `<td style="text-align:right">${_esc(_fmtCost(m.cost))}</td>`;
        const modelLabel = _esc(m.model || '—') + (m.escalated ? ' <span style="color:var(--muted);font-size:.75em">(escalated)</span>' : '');
        return `<tr><td>${modelLabel}</td><td style="text-align:right">${_fmtNum(m.analyses||0)}</td>${tokensHtml}${costHtml}</tr>`;
      }).join('');
    }

    // Populate daily usage history
    const dailyTbody = $('usage-daily-tbody');
    const dailyNoData = $('usage-daily-no-data');
    const daily = d.daily || [];
    if (daily.length === 0) {
      dailyTbody.innerHTML = '';
      dailyNoData.style.display = '';
    } else {
      dailyNoData.style.display = 'none';
      dailyTbody.innerHTML = daily.map(row => {
        return `<tr><td>${_esc(row.day || '—')}</td>` +
          `<td style="text-align:right">${_fmtNum(row.analyses||0)}</td>` +
          `<td style="text-align:right">${_fmtNum(row.tokens_total||0)}</td>` +
          `<td style="text-align:right">${_esc(_fmtCost(row.cost))}</td></tr>`;
      }).join('');
    }

    $('usage-disabled-msg').style.display = (!d.enabled && totalAnalyses === 0) ? 'block' : 'none';
    $('usage-content').style.display = '';
  } catch(e) {
    console.error('AI usage error', e);
  }
}

$('usage-clear-btn').addEventListener('click', async () => {
  const ok = await showConfirmModal(
    'Clear all AI usage stats (tokens, cost, escalations)? Per-clip analysis results are not affected.',
    'Clear AI usage stats?'
  );
  if (!ok) return;
  try {
    await api('/api/ai/usage', { method: 'DELETE' });
    toast('AI usage stats cleared');
    loadAIUsage();
  } catch (e) {
    toast('Failed to clear usage stats', true);
  }
});
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# MediaServer
# ---------------------------------------------------------------------------


class MediaServer:
    """aiohttp web server: clip library REST API + Video.js browser UI."""

    def __init__(
        self,
        db: ClipDatabase,
        download_path: Path,
        port: int,
        trigger_download: Callable[[], Awaitable[None]] | None = None,
        two_fa_callback: Callable[[str], int] | None = None,
        auth_state_getter: Callable[[], dict] | None = None,
        analyzer: BaseAnalyzer | None = None,
        analysis_queue: AnalysisQueue | None = None,
        notification_dispatcher: NotificationDispatcher | None = None,
        moondream_api_key: str = "",
        prompt_debug_enabled: bool = False,
    ) -> None:
        self._db = db
        self._download_path = download_path
        self._port = port
        self._trigger_download = trigger_download
        self._two_fa_callback = two_fa_callback
        self._auth_state_getter = auth_state_getter
        self._analyzer = analyzer
        self._analysis_queue = analysis_queue
        self._notification_dispatcher = notification_dispatcher
        # Used only to stand up a MoondreamFineTuneManager for the Fine-Tuning
        # API/panel when provider == "moondream_cloud" — see _handle_finetune_*.
        self._moondream_api_key = moondream_api_key
        # Gates whether /api/ai/status advertises the feature and whether
        # /api/ai/results/{clip_id} ever includes prompt_text — see
        # ai_prompt_debug_enabled. Off means fully hidden, not just
        # unpopulated, even if a prompt happens to be stored from when the
        # feature was previously on.
        self._prompt_debug_enabled = prompt_debug_enabled
        self._runner: web.AppRunner | None = None
        self.extra_status: dict = {}
        # Holds a strong reference to the background moondream-install task —
        # asyncio only keeps a weak reference internally, so an unreferenced
        # task can be garbage-collected mid-install.
        self._moondream_install_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        app = self._build_app()
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        _LOGGER.info("Media server listening on port %d", self._port)

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()
            self._runner = None

    # ------------------------------------------------------------------
    # App factory
    # ------------------------------------------------------------------

    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[_security_middleware])
        app.router.add_get("/", self._handle_index)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/api/clips", self._handle_list_clips)
        app.router.add_get("/api/clips/{id}", self._handle_get_clip)
        app.router.add_delete("/api/clips/{id}", self._handle_delete_clip)
        app.router.add_put("/api/clips/{id}/star", self._handle_star_clip)
        app.router.add_put("/api/clips/{id}/tags", self._handle_set_tags)
        app.router.add_get("/api/clips/{id}/stream", self._handle_stream)
        app.router.add_get("/api/clips/{id}/thumb", self._handle_thumbnail)
        app.router.add_get("/api/cameras", self._handle_cameras)
        app.router.add_get("/api/stats", self._handle_stats)
        app.router.add_get("/api/activity", self._handle_activity)
        app.router.add_get("/api/tags", self._handle_tags)
        app.router.add_post("/api/clips/export-zip", self._handle_export_zip)
        app.router.add_post("/api/download-now", self._handle_download_now)
        app.router.add_get("/api/auth/status", self._handle_auth_status)
        app.router.add_post("/api/auth/2fa", self._handle_two_fa)
        # AI Analysis endpoints
        app.router.add_get("/api/ai/status", self._handle_ai_status)
        app.router.add_get("/api/ai/usage", self._handle_ai_usage)
        app.router.add_delete("/api/ai/usage", self._handle_ai_usage_clear)
        app.router.add_get("/api/ai/models", self._handle_ai_models)
        app.router.add_get("/api/ai/queue", self._handle_ai_queue)
        app.router.add_get("/api/ai/results/{clip_id}", self._handle_ai_clip_result)
        app.router.add_get("/api/ai/suspicious", self._handle_ai_suspicious)
        app.router.add_post("/api/ai/analyze/{clip_id}", self._handle_ai_analyze_now)
        app.router.add_post("/api/ai/test", self._handle_ai_test)
        app.router.add_get(
            "/api/ai/moondream/install-status", self._handle_moondream_install_status
        )
        app.router.add_post("/api/ai/moondream/install", self._handle_moondream_install)
        app.router.add_get("/api/ai/camera-configs", self._handle_ai_camera_configs_get)
        app.router.add_put("/api/ai/camera-configs", self._handle_ai_camera_configs_put)
        # Adaptive learning (feedback) endpoints
        app.router.add_get("/api/ai/feedback/stats", self._handle_ai_feedback_stats)
        app.router.add_get("/api/ai/feedback/{clip_id}", self._handle_ai_feedback_get)
        app.router.add_post(
            "/api/ai/feedback/{clip_id}", self._handle_ai_feedback_submit
        )
        # Moondream Cloud fine-tuning endpoints
        app.router.add_get("/api/ai/finetune", self._handle_finetune_list)
        app.router.add_post("/api/ai/finetune", self._handle_finetune_create)
        app.router.add_get("/api/ai/finetune/{finetune_id}", self._handle_finetune_get)
        app.router.add_delete(
            "/api/ai/finetune/{finetune_id}", self._handle_finetune_delete
        )
        app.router.add_get(
            "/api/ai/finetune/{finetune_id}/checkpoints",
            self._handle_finetune_checkpoints,
        )
        app.router.add_post(
            "/api/ai/finetune/{finetune_id}/activate", self._handle_finetune_activate
        )
        app.router.add_post("/api/notifications/test-email", self._handle_test_email)
        return app

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.Response:
        # HA ingress sends X-Ingress-Path so the JS can prefix all API calls.
        # For direct port access the header is absent and the prefix is empty.
        # The header value is attacker-controlled on any deployment where a
        # client can set arbitrary request headers, so it must never be
        # interpolated into the page verbatim: json.dumps() produces a
        # properly quote/backslash-escaped JS string literal, and the
        # "</" -> "<\/" swap additionally prevents a value like
        # "</script><script>..." from closing out the surrounding <script>
        # tag early.
        ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
        safe_literal = json.dumps(ingress_path).replace("</", "<\\/")
        html = _HTML.replace("'__HAROOT__'", safe_literal)
        return web.Response(text=html, content_type="text/html")

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_list_clips(self, request: web.Request) -> web.Response:
        q = request.rel_url.query
        try:
            # A negative SQLite LIMIT means "no limit", and a negative OFFSET
            # is invalid - clamp both to non-negative so a crafted query
            # string can't bypass pagination and dump the whole table.
            limit = max(0, min(int(q.get("limit", 48)), 200))
            offset = max(0, int(q.get("offset", 0)))
        except ValueError:
            limit, offset = 48, 0

        starred_raw = q.get("starred")
        starred = True if starred_raw == "1" else False if starred_raw == "0" else None
        notified_only = q.get("notified") == "1"
        min_confidence = (
            self._analysis_queue.min_confidence if self._analysis_queue else 0.0
        )

        clips = await self._db.get_clips(
            camera=q.get("camera") or None,
            since=q.get("since") or None,
            until=q.get("until") or None,
            starred=starred,
            source=q.get("source") or None,
            tag=q.get("tag") or None,
            search=q.get("search") or None,
            sort=q.get("sort") or "newest",
            limit=limit,
            offset=offset,
            notified_only=notified_only,
            min_confidence=min_confidence,
        )
        return web.json_response(clips)

    async def _handle_get_clip(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        return web.json_response(clip)

    async def _handle_delete_clip(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        file_path = Path(clip["file_path"])
        if file_path.exists():
            try:
                file_path.unlink()
                thumb = file_path.with_suffix(".jpg")
                if thumb.exists():
                    thumb.unlink()
            except OSError as exc:
                _LOGGER.warning("Could not delete file %s: %s", file_path, exc)
        await self._db.delete_clip(clip_id)
        return web.json_response({"deleted": True})

    async def _handle_star_clip(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        try:
            body = await request.json()
            starred = bool(body.get("starred", True))
        except Exception:  # noqa: BLE001
            starred = True
        found = await self._db.star_clip(clip_id, starred)
        if not found:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        return web.json_response({"id": clip_id, "starred": starred})

    async def _handle_set_tags(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        try:
            body = await request.json()
            tags = [str(t) for t in body.get("tags", [])]
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")
        found = await self._db.set_tags(clip_id, tags)
        if not found:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)
        return web.json_response({"id": clip_id, "tags": tags})

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)

        file_path = Path(clip["file_path"])
        if not file_path.exists():
            raise web.HTTPNotFound(text="Clip file not found on disk")

        # aiohttp's FileResponse uses the OS sendfile() syscall on Linux,
        # bypassing the Python interpreter for the actual byte transfer.
        # It automatically handles Range requests (206 Partial Content),
        # ETag/Last-Modified caching, and correct Accept-Ranges headers —
        # all of which contribute to stutter-free video seeking on the Pi.
        return web.FileResponse(
            file_path,
            chunk_size=262_144,
            headers={
                "Content-Disposition": f'inline; filename="{file_path.name}"',
                # Allow the browser to cache video segments so re-seeking an
                # already-watched section never round-trips to the server.
                "Cache-Control": "public, max-age=3600",
            },
        )

    async def _handle_thumbnail(self, request: web.Request) -> web.StreamResponse:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound()

        thumb = Path(clip["file_path"]).with_suffix(".jpg")
        if thumb.exists():
            return web.FileResponse(
                thumb,
                headers={"Cache-Control": "public, max-age=3600"},
            )

        raise web.HTTPNotFound(text="Thumbnail not available")

    async def _handle_cameras(self, _request: web.Request) -> web.Response:
        camera_stats = await self._db.get_camera_stats()
        return web.json_response(camera_stats)

    async def _handle_stats(self, request: web.Request) -> web.Response:
        stats = await self._db.get_stats()
        # extra_status is MediaServer's own dict (populated by app.py after
        # each poll cycle).  Do NOT read from request.app — that is aiohttp's
        # internal Application dict and is never populated with disk_stats.
        disk_raw = self.extra_status.get("disk")
        if disk_raw:
            stats["disk"] = disk_raw
        stats.update(self.extra_status)
        return web.json_response(stats)

    async def _handle_activity(self, request: web.Request) -> web.Response:
        try:
            # A zero/negative `days` shifts get_activity_data()'s cutoff to
            # today or into the future, silently returning no data instead of
            # erroring — clamp the lower bound like the other paginated
            # endpoints in this file (_handle_list_clips, _handle_ai_suspicious)
            # already do for limit/offset.
            days = max(1, min(int(request.rel_url.query.get("days", 7)), 30))
        except ValueError:
            days = 7
        data = await self._db.get_activity_data(days)
        return web.json_response(data)

    async def _handle_tags(self, _request: web.Request) -> web.Response:
        tags = await self._db.get_distinct_tags()
        return web.json_response(tags)

    async def _handle_export_zip(self, request: web.Request) -> web.Response:
        """Package up to 25 selected clips into a ZIP and return it."""
        try:
            body = await request.json()
            clip_ids = [str(c) for c in body.get("ids", [])][:25]
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid request body")

        if not clip_ids:
            raise web.HTTPBadRequest(text="No clip IDs provided")

        buf = io.BytesIO()
        added = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for cid in clip_ids:
                clip = await self._db.get_clip(cid)
                if not clip:
                    continue
                fp = Path(clip["file_path"])
                if fp.exists():
                    zf.write(fp, fp.name)
                    added += 1

        if not added:
            raise web.HTTPNotFound(text="No clip files found on disk")

        buf.seek(0)
        return web.Response(
            body=buf.read(),
            content_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="blink-clips.zip"'},
        )

    async def _handle_auth_status(self, _request: web.Request) -> web.Response:
        if self._auth_state_getter:
            status = self._auth_state_getter()
        else:
            status = {"state": "connected", "message": ""}
        return web.json_response(status)

    async def _handle_two_fa(self, request: web.Request) -> web.Response:
        if not self._two_fa_callback:
            raise web.HTTPServiceUnavailable(text="2FA not available")
        try:
            body = await request.json()
            code = str(body.get("code", "")).strip()
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid request body")
        if not code.isdigit() or len(code) != 6:
            raise web.HTTPBadRequest(text="Code must be exactly 6 digits")
        seq = self._two_fa_callback(code)
        return web.json_response({"submitted": True, "seq": seq})

    async def _handle_download_now(self, _request: web.Request) -> web.Response:
        if self._trigger_download:
            await self._trigger_download()
            return web.json_response({"triggered": True})
        try:
            Path("/data/trigger_download").touch()
        except OSError:
            pass
        return web.json_response({"triggered": True})

    # ------------------------------------------------------------------
    # AI Analysis handlers
    # ------------------------------------------------------------------

    async def _handle_ai_status(self, _request: web.Request) -> web.Response:
        enabled = self._analyzer is not None
        data: dict = {
            "enabled": enabled,
            "prompt_debug_enabled": self._prompt_debug_enabled,
        }
        if enabled:
            assert self._analyzer is not None
            data["ai_online"] = await self._analyzer.health_check()
            data["provider"] = self._analyzer.provider_name
            data["model"] = self._analyzer.model_name()
            data["car_protection_active"] = self._analyzer.car_protection_active
            if self._analyzer.provider_name == "moondream_local":
                data["moondream_installed"] = _is_moondream_installed()
                data["moondream_arch_supported"] = _moondream_arch_supported()
            escalation = self._analyzer.escalation_analyzer
            if escalation is not None:
                data["escalation_provider"] = escalation.provider_name
                data["escalation_model"] = escalation.model_name()
                # A misconfigured tier 2 (e.g. wrong API key) should be
                # visible here before it silently falls back on every
                # suspicious clip — see BaseAnalyzer._maybe_escalate.
                data["escalation_online"] = await escalation.health_check()
        data["smtp_configured"] = bool(
            self._notification_dispatcher
            and self._notification_dispatcher.smtp_configured
        )
        if self._analysis_queue:
            data["queue"] = await self._analysis_queue.get_queue_status()
        data["analysis_stats"] = await self._db.get_analysis_stats()
        return web.json_response(data)

    async def _handle_ai_usage(self, _request: web.Request) -> web.Response:
        from .analyzer import lookup_model_pricing  # noqa: PLC0415

        enabled = self._analyzer is not None
        data: dict = {"enabled": enabled}
        if enabled:
            assert self._analyzer is not None
            data["provider"] = self._analyzer.provider_name
            data["model"] = self._analyzer.model_name()
            if hasattr(self._analyzer, "model_pricing"):
                inp, out = self._analyzer.model_pricing()  # type: ignore[union-attr]
                data["cost_per_1m_input"] = inp
                data["cost_per_1m_output"] = out
        usage = await self._db.get_token_usage_stats()

        # Price each model row against its *own* pricing table entry (rather
        # than the blanket "current model" rate above) so a per-model
        # breakdown that spans an escalation model, or leftover rows from a
        # provider the user has since switched away from, isn't priced as if
        # every token cost what the active model costs.
        total_cost = 0.0
        any_priced = False
        for row in usage.get("by_model", []):
            pricing = lookup_model_pricing(row.get("model", ""))
            if pricing is None:
                row["cost"] = None
                continue
            inp, out = pricing
            row_cost = (
                int(row.get("tokens_prompt") or 0) * inp
                + int(row.get("tokens_completion") or 0) * out
            ) / 1_000_000
            row["cost"] = row_cost
            total_cost += row_cost
            any_priced = True
        usage["total_estimated_cost"] = total_cost if any_priced else None

        # Daily history (last 14 days): each (day, model) row from the DB is
        # priced individually — same reasoning as by_model above — then
        # collapsed into one total per day so the UI renders a small,
        # fixed-size table instead of a per-model breakdown per day.
        daily_totals: dict[str, dict[str, Any]] = {}
        for row in await self._db.get_daily_usage_stats(days=14):
            day = str(row["day"])
            entry = daily_totals.setdefault(
                day,
                {
                    "day": day,
                    "analyses": 0,
                    "tokens_prompt": 0,
                    "tokens_completion": 0,
                    "cost": 0.0,
                    "any_priced": False,
                },
            )
            tp = int(row.get("tokens_prompt") or 0)
            tc = int(row.get("tokens_completion") or 0)
            if not row.get("escalated"):
                entry["analyses"] += int(row.get("analyses") or 0)
            entry["tokens_prompt"] += tp
            entry["tokens_completion"] += tc
            pricing = lookup_model_pricing(row.get("model", ""))
            if pricing is not None:
                inp, out = pricing
                entry["cost"] += (tp * inp + tc * out) / 1_000_000
                entry["any_priced"] = True

        data["daily"] = [
            {
                "day": e["day"],
                "analyses": e["analyses"],
                "tokens_prompt": e["tokens_prompt"],
                "tokens_completion": e["tokens_completion"],
                "tokens_total": e["tokens_prompt"] + e["tokens_completion"],
                "cost": e["cost"] if e["any_priced"] else None,
            }
            for e in sorted(daily_totals.values(), key=lambda e: e["day"], reverse=True)
        ]

        data.update(usage)
        return web.json_response(data)

    async def _handle_ai_usage_clear(self, _request: web.Request) -> web.Response:
        await self._db.clear_ai_usage_stats()
        return web.json_response({"cleared": True})

    async def _handle_ai_models(self, _request: web.Request) -> web.Response:
        if not self._analyzer:
            return web.json_response({"enabled": False, "models": []})
        models = await self._analyzer.fetch_models()
        return web.json_response({"enabled": True, "models": models})

    async def _handle_ai_queue(self, _request: web.Request) -> web.Response:
        if not self._analysis_queue:
            return web.json_response({"enabled": False})
        status = await self._analysis_queue.get_queue_status()
        return web.json_response({"enabled": True, **status})

    async def _handle_ai_clip_result(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["clip_id"]
        result = await self._db.get_analysis_for_clip(clip_id)
        if not result:
            return web.json_response(None)
        if not self._prompt_debug_enabled:
            # Off means fully hidden — even a clip analyzed while the
            # feature was previously on must not leak its stored prompt_text
            # once the admin has turned this back off.
            result.pop("prompt_text", None)
        return web.json_response(result)

    async def _handle_ai_suspicious(self, request: web.Request) -> web.Response:
        q = request.rel_url.query
        try:
            limit = max(0, min(int(q.get("limit", 50)), 200))
            offset = max(0, int(q.get("offset", 0)))
        except ValueError:
            limit, offset = 50, 0
        results = await self._db.get_suspicious_clips(limit=limit, offset=offset)
        return web.json_response(results)

    async def _handle_ai_analyze_now(self, request: web.Request) -> web.Response:
        if not self._analyzer:
            return web.json_response(
                {"error": "AI analysis not configured"}, status=400
            )
        clip_id = request.match_info["clip_id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text=_CLIP_NOT_FOUND)

        try:
            result = await self._analyzer.analyze_clip(
                clip_path=clip["file_path"],
                clip_id=clip_id,
                camera=clip["camera"],
                clip_duration=float(clip.get("duration") or 0),
            )
            await self._db.add_analysis_result(result.to_dict())
            return web.json_response(result.to_dict())
        except Exception as exc:  # noqa: BLE001
            # Mirrors _handle_ai_test's error handling — without this, an
            # unexpected failure here would surface as aiohttp's generic
            # HTML 500 page instead of the {"error": ...} JSON contract the
            # rest of the AI API uses, breaking the web UI's error display.
            _LOGGER.warning("AI analyze-now failed for clip %s: %s", clip_id, exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_ai_test(self, _request: web.Request) -> web.Response:
        """Test AI by analyzing the most recently downloaded clip."""
        if not self._analyzer:
            return web.json_response(
                {"error": "AI analysis not configured"}, status=400
            )
        clips = await self._db.get_clips(limit=1, sort="newest")
        if not clips:
            return web.json_response(
                {"error": "No clips in library — download a clip first"},
                status=404,
            )
        clip = clips[0]
        try:
            result = await self._analyzer.analyze_clip(
                clip_path=clip["file_path"],
                clip_id=clip["id"],
                camera=clip["camera"],
                clip_duration=float(clip.get("duration") or 0),
            )
            await self._db.add_analysis_result(result.to_dict())
            return web.json_response(
                {"success": True, "clip_id": clip["id"], **result.to_dict()}
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("AI test analysis failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)

    async def _handle_test_email(self, _request: web.Request) -> web.Response:
        """Send a one-off test email using the configured SMTP settings."""
        if not self._notification_dispatcher:
            return web.json_response(
                {"success": False, "message": "Notifications not configured"},
                status=400,
            )
        ok, message = await self._notification_dispatcher.send_test_email()
        return web.json_response(
            {"success": ok, "message": message}, status=200 if ok else 400
        )

    async def _handle_moondream_install_status(
        self, _request: web.Request
    ) -> web.Response:
        return web.json_response(
            {
                "installed": _is_moondream_installed(),
                "arch_supported": _moondream_arch_supported(),
                "install_state": _moondream_install_state.copy(),
            }
        )

    async def _handle_moondream_install(self, _request: web.Request) -> web.Response:
        global _moondream_install_state  # noqa: PLW0603

        if not _moondream_arch_supported():
            return web.json_response(
                {
                    "status": "unsupported",
                    "log": (
                        f"moondream_local is not supported on {platform.machine()} "
                        "(no pre-built wheels for this architecture). "
                        "Use moondream_cloud or ollama instead."
                    ),
                },
                status=422,
            )

        if _is_moondream_installed():
            return web.json_response({"status": "already_installed"})

        if _moondream_install_state.get("status") == "installing":
            return web.json_response(
                {"status": "installing", "log": _moondream_install_state.get("log", "")}
            )

        try:
            _MOONDREAM_PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            _LOGGER.warning("Could not create moondream packages dir: %s", exc)
        _moondream_install_state = {
            "status": "installing",
            "log": (
                f"Starting: pip install --target {_MOONDREAM_PACKAGES_DIR} "
                f"{_MOONDREAM_PIP_SPEC}\n"
            ),
        }

        async def _run_install() -> None:
            global _moondream_install_state  # noqa: PLW0603
            try:
                proc = await asyncio.create_subprocess_exec(
                    "pip3",
                    "install",
                    "--no-cache-dir",
                    "--target",
                    str(_MOONDREAM_PACKAGES_DIR),
                    _MOONDREAM_PIP_SPEC,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                )
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=900)
                log = stdout.decode(errors="replace") if stdout else ""
                if proc.returncode == 0:
                    pkg = str(_MOONDREAM_PACKAGES_DIR)
                    if pkg not in sys.path:
                        sys.path.insert(0, pkg)
                    _moondream_install_state = {"status": "installed", "log": log}
                    _LOGGER.info(
                        "moondream installed successfully to %s",
                        _MOONDREAM_PACKAGES_DIR,
                    )
                else:
                    _moondream_install_state = {"status": "failed", "log": log}
                    _LOGGER.warning("moondream install failed (rc=%d)", proc.returncode)
            except asyncio.TimeoutError:
                _moondream_install_state = {
                    "status": "failed",
                    "log": "Installation timed out after 15 minutes",
                }
            except Exception as exc:  # noqa: BLE001
                _moondream_install_state = {"status": "failed", "log": str(exc)}

        self._moondream_install_task = asyncio.create_task(_run_install())
        return web.json_response({"status": "installing"})

    _CAMERA_CONFIGS_FILE = Path("/data/camera_configs.json")

    async def _handle_ai_camera_configs_get(
        self, _request: web.Request
    ) -> web.Response:
        """Return current per-camera AI configurations."""
        cameras = await self._db.get_camera_stats()
        cam_names = [c["camera"] for c in cameras]
        configs: list[dict] = []
        if self._CAMERA_CONFIGS_FILE.exists():
            try:
                import json as _json  # noqa: PLC0415

                configs = _json.loads(self._CAMERA_CONFIGS_FILE.read_text())
            except Exception:  # noqa: BLE001
                configs = []
        # Ensure every known camera has an entry
        configured = {c.get("camera", ""): c for c in configs}
        result = []
        for name in cam_names:
            entry = configured.get(
                name,
                {
                    "camera": name,
                    "description": "",
                    "custom_prompt": "",
                    "is_car_camera": False,
                    "car_zone": None,
                },
            )
            result.append(
                {
                    "camera": name,
                    "description": str(entry.get("description", "")),
                    "custom_prompt": str(entry.get("custom_prompt", "")),
                    "is_car_camera": bool(entry.get("is_car_camera", False)),
                    "car_zone": self._normalize_car_zone(entry.get("car_zone")),
                }
            )
        # Also include configured cameras not in the current clip list
        for name, entry in configured.items():
            if name not in cam_names:
                result.append(
                    {
                        "camera": name,
                        "description": str(entry.get("description", "")),
                        "custom_prompt": str(entry.get("custom_prompt", "")),
                        "is_car_camera": bool(entry.get("is_car_camera", False)),
                        "car_zone": self._normalize_car_zone(entry.get("car_zone")),
                    }
                )
        return web.json_response(result)

    @staticmethod
    def _normalize_car_zone(zone: Any) -> dict[str, float] | None:
        """Validate and coerce a raw ``car_zone`` value from stored/incoming
        JSON into a clean ``{x_min, y_min, x_max, y_max}`` dict, or ``None``
        if it's missing, malformed, or not a sane rectangle (min >= max).
        """
        if not isinstance(zone, dict):
            return None
        try:
            x_min, y_min = float(zone["x_min"]), float(zone["y_min"])
            x_max, y_max = float(zone["x_max"]), float(zone["y_max"])
        except (KeyError, TypeError, ValueError):
            return None
        if not (0.0 <= x_min < x_max <= 1.0 and 0.0 <= y_min < y_max <= 1.0):
            return None
        return {"x_min": x_min, "y_min": y_min, "x_max": x_max, "y_max": y_max}

    async def _handle_ai_camera_configs_put(self, request: web.Request) -> web.Response:
        """Save per-camera AI configurations and update the live analyzer."""
        try:
            body = await request.json()
            configs = [
                {
                    "camera": str(c["camera"]),
                    "description": str(c.get("description", "")),
                    "custom_prompt": str(c.get("custom_prompt", "")),
                    "is_car_camera": bool(c.get("is_car_camera", False)),
                    "car_zone": self._normalize_car_zone(c.get("car_zone")),
                }
                for c in body
                if isinstance(c, dict) and c.get("camera")
            ]
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")

        try:
            import json as _json  # noqa: PLC0415

            self._CAMERA_CONFIGS_FILE.write_text(_json.dumps(configs, indent=2))
        except OSError as exc:
            _LOGGER.warning("Could not save camera configs: %s", exc)

        # Update the live analyzer without restart. Every field is a full
        # replace, not a merge — camera_configs.json is the single source of
        # truth for these settings (see CLAUDE.md), so clearing a value in
        # the AI tab must stop it from applying immediately rather than
        # leaving the last non-empty value in place until a restart.
        if self._analyzer is not None:
            descriptions = {
                c["camera"]: c["description"] for c in configs if c.get("description")
            }
            self._analyzer.update_camera_descriptions(descriptions)
            prompts = {
                c["camera"]: c["custom_prompt"]
                for c in configs
                if c.get("custom_prompt")
            }
            self._analyzer.update_camera_prompts(prompts)
            car_cameras = {c["camera"] for c in configs if c.get("is_car_camera")}
            self._analyzer.update_car_cameras(car_cameras)
            car_zones = {
                c["camera"]: c["car_zone"] for c in configs if c.get("car_zone")
            }
            self._analyzer.update_car_zones(car_zones)

        return web.json_response({"saved": True, "count": len(configs)})

    # ------------------------------------------------------------------
    # Adaptive learning (human feedback on AI verdicts)
    # ------------------------------------------------------------------

    async def _handle_ai_feedback_stats(self, request: web.Request) -> web.Response:
        camera = request.rel_url.query.get("camera") or None
        stats = await self._db.get_feedback_stats(camera)
        return web.json_response(stats)

    async def _handle_ai_feedback_get(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["clip_id"]
        feedback = await self._db.get_feedback_for_clip(clip_id)
        return web.json_response(feedback)

    async def _handle_ai_feedback_submit(self, request: web.Request) -> web.Response:
        """Record feedback on a clip's stored AI verdict.

        Body: ``{"correct": bool, "correction_note": str,
        "corrected_suspicious": true|false|null}``. Requires the clip to
        already have a stored analysis result — feedback is a correction on
        an existing verdict, not a substitute for one.
        """
        clip_id = request.match_info["clip_id"]
        try:
            body = await request.json()
            correct = bool(body.get("correct"))
            correction_note = str(body.get("correction_note", "") or "")
            corrected_suspicious = body.get("corrected_suspicious")
            if corrected_suspicious is not None:
                corrected_suspicious = bool(corrected_suspicious)
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")

        result = await self._db.get_analysis_for_clip(clip_id)
        if not result:
            return web.json_response(
                {"error": "Clip has not been analyzed yet"}, status=400
            )

        # A bare thumbs-down with no typed note carries no reusable signal
        # for get_prompt_corrections (see database.py), which only folds in
        # rows with a non-empty correction_note. Synthesize one from the
        # direction of the correction so every "incorrect" rating still
        # becomes usable few-shot guidance for future clips on this camera.
        if not correct and not correction_note.strip():
            correction_note = (
                "Reviewer marked this as ordinary, routine activity that "
                "was incorrectly flagged suspicious."
                if result["is_suspicious"]
                else "Reviewer marked this as genuinely suspicious activity "
                "that was incorrectly cleared."
            )

        try:
            await self._db.add_feedback(
                clip_id=clip_id,
                camera=result["camera"],
                analysis_result_id=result.get("id"),
                original_suspicious=bool(result["is_suspicious"]),
                original_confidence=float(result["confidence"]),
                correct=correct,
                correction_note=correction_note,
                corrected_suspicious=corrected_suspicious,
            )
            return web.json_response({"saved": True})
        except Exception as exc:  # noqa: BLE001
            # Mirrors _handle_ai_analyze_now's error handling — an unexpected
            # DB failure here must surface as clean JSON, not aiohttp's
            # generic HTML 500 page.
            _LOGGER.warning("Feedback submit failed for clip %s: %s", clip_id, exc)
            return web.json_response({"error": str(exc)}, status=500)

    # ------------------------------------------------------------------
    # Moondream Cloud fine-tuning
    # ------------------------------------------------------------------

    def _get_finetune_manager(self) -> MoondreamFineTuneManager | None:
        """Return a fine-tune manager, or None if not configured for it.

        Only meaningful when the active provider is moondream_cloud — the
        only one of the six providers with a fine-tuning API (see the
        module docstring and CHANGELOG for why OpenAI/Anthropic aren't
        supported here).
        """
        if (
            self._analyzer is None
            or self._analyzer.provider_name != "moondream_cloud"
            or not self._moondream_api_key
        ):
            return None
        from .analyzer import MoondreamFineTuneManager  # noqa: PLC0415

        return MoondreamFineTuneManager(api_key=self._moondream_api_key)

    async def _handle_finetune_list(self, _request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response({"enabled": False, "finetunes": []})
        try:
            finetunes = await manager.list_finetunes()
            return web.json_response({"enabled": True, "finetunes": finetunes})
        finally:
            await manager.close()

    async def _handle_finetune_create(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response(
                {"error": "Fine-tuning requires ai_provider=moondream_cloud"},
                status=400,
            )
        try:
            body = await request.json()
            name = str(body.get("name", "") or "").strip()
            rank = int(body.get("rank", 16))
        except Exception:  # noqa: BLE001
            await manager.close()
            raise web.HTTPBadRequest(text="Invalid JSON body")

        if not name:
            await manager.close()
            return web.json_response({"error": "name is required"}, status=400)

        try:
            finetune_id = await manager.create_finetune(name, rank=rank)
            if finetune_id is None:
                return web.json_response(
                    {"error": "Failed to create fine-tune"}, status=500
                )
            return web.json_response({"finetune_id": finetune_id})
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Moondream create_finetune failed: %s", exc)
            return web.json_response({"error": str(exc)}, status=500)
        finally:
            await manager.close()

    async def _handle_finetune_get(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response(
                {"error": "Fine-tuning not configured"}, status=400
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            finetune = await manager.get_finetune(finetune_id)
            if finetune is None:
                raise web.HTTPNotFound(text="Fine-tune not found")
            return web.json_response(finetune)
        finally:
            await manager.close()

    async def _handle_finetune_delete(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response(
                {"error": "Fine-tuning not configured"}, status=400
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            deleted = await manager.delete_finetune(finetune_id)
            return web.json_response({"deleted": deleted})
        finally:
            await manager.close()

    async def _handle_finetune_checkpoints(self, request: web.Request) -> web.Response:
        manager = self._get_finetune_manager()
        if manager is None:
            return web.json_response({"enabled": False, "checkpoints": []})
        finetune_id = request.match_info["finetune_id"]
        try:
            checkpoints = await manager.list_checkpoints(finetune_id)
            return web.json_response({"enabled": True, "checkpoints": checkpoints})
        finally:
            await manager.close()

    async def _handle_finetune_activate(self, request: web.Request) -> web.Response:
        """Switch live inference to a fine-tuned checkpoint, no restart.

        Body: ``{"step": int}``. Only valid when the active analyzer is a
        MoondreamCloudAnalyzer (checked via _get_finetune_manager's
        provider_name gate, but the hot-swap itself needs the concrete
        analyzer instance, not just the manager).
        """
        from .analyzer import MoondreamCloudAnalyzer, MoondreamFineTuneManager  # noqa: PLC0415

        if self._analyzer is None or not isinstance(
            self._analyzer, MoondreamCloudAnalyzer
        ):
            return web.json_response(
                {"error": "Fine-tuning requires ai_provider=moondream_cloud"},
                status=400,
            )
        finetune_id = request.match_info["finetune_id"]
        try:
            body = await request.json()
            step = int(body.get("step"))
        except Exception:  # noqa: BLE001
            raise web.HTTPBadRequest(text="Invalid JSON body")

        model_id = MoondreamFineTuneManager.get_model_id(finetune_id, step)
        self._analyzer.set_finetune_model(model_id)
        return web.json_response({"activated": True, "model": model_id})
