"""HTTP media server: REST API + embedded SPA with Video.js media player."""

from __future__ import annotations

import io
import logging
import zipfile
from pathlib import Path
from typing import Awaitable, Callable

from aiohttp import web

from .database import ClipDatabase

_LOGGER = logging.getLogger(__name__)

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
/* Light theme (default – matches HA's default light UI) */
:root{
  --bg:#f3f4f6;--surface:#ffffff;--card:#f9fafb;--card2:#f0f2f5;
  --border:#e5e7eb;--accent:#2563eb;--accent2:#1d4ed8;--success:#16a34a;
  --danger:#dc2626;--warn:#d97706;--text:#111827;--muted:#6b7280;
  --starred:#f59e0b;--radius:8px;--nav-h:56px;
  --btn-text:#ffffff;--badge-ok-bg:#dcfce7;--badge-err-bg:#fee2e2;
  --tag-bg:#dbeafe;--tag-text:#1d4ed8;--code-color:#1e40af
}
/* Dark theme – activates when OS/browser prefers dark (matches HA dark theme) */
@media(prefers-color-scheme:dark){:root{
  --bg:#0d1117;--surface:#161b22;--card:#1c2128;--card2:#21262d;
  --border:#30363d;--accent:#58a6ff;--accent2:#1f6feb;--success:#3fb950;
  --danger:#f85149;--warn:#d29922;--text:#c9d1d9;--muted:#8b949e;
  --starred:#e3b341;
  --btn-text:#0d1117;--badge-ok-bg:#0d2818;--badge-err-bg:#2d1313;
  --tag-bg:#1a3055;--tag-text:#79b8ff;--code-color:#a9d1f7
}}
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
     background:var(--bg);color:var(--text);height:100vh;display:flex;
     flex-direction:column;overflow:hidden}
button,input,select{font:inherit}
a{color:var(--accent);text-decoration:none}
code{background:var(--card2);border:1px solid var(--border);border-radius:4px;
     padding:.1em .4em;font-family:monospace;font-size:.85em}

/* ── Navigation ──────────────────────────────────────── */
.nav{background:var(--surface);border-bottom:1px solid var(--border);
     height:var(--nav-h);display:flex;align-items:center;gap:.5rem;
     padding:0 1rem;flex-shrink:0;z-index:10}
.nav-brand{font-size:1.05rem;font-weight:700;color:var(--accent);
           white-space:nowrap;margin-right:.5rem}
.nav-brand span{opacity:.5;font-weight:400}
.nav-tabs{display:flex;gap:.2rem;flex:1}
.nav-tab{background:transparent;border:none;color:var(--muted);
         padding:.4rem .8rem;border-radius:var(--radius);cursor:pointer;
         font-size:.88rem;font-weight:500;transition:.15s;white-space:nowrap}
.nav-tab:hover{color:var(--text);background:var(--card)}
.nav-tab.active{color:var(--accent);background:var(--card2)}
.nav-actions{display:flex;align-items:center;gap:.45rem;margin-left:auto}

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
.clip-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(228px,1fr));gap:.8rem}
.clip-card{background:var(--card);border:1px solid var(--border);border-radius:var(--radius);
           overflow:hidden;cursor:pointer;transition:.15s;position:relative;user-select:none}
.clip-card:hover{border-color:var(--accent2);transform:translateY(-2px);
                 box-shadow:0 4px 16px rgba(31,111,235,.2)}
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
.modal-bg.open{display:flex}
.modal{background:var(--surface);border:1px solid var(--border);
       border-radius:12px;max-width:980px;width:100%;margin:auto;
       overflow:hidden;position:relative}
.modal.theater{max-width:100%;border-radius:0;border:none}

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

/* ── Status page ──────────────────────────────────────── */
#page-status{overflow-y:auto;padding:1.5rem}
.status-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));
             gap:1rem;max-width:1050px;margin:0 auto}
.status-card{background:var(--card);border:1px solid var(--border);
             border-radius:var(--radius);padding:1rem 1.15rem}
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
#page-automations{overflow-y:auto;padding:1.5rem}
.auto-content{max-width:820px;margin:0 auto}
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

/* ── Toast ────────────────────────────────────────────── */
.toast{position:fixed;bottom:1.4rem;right:1.4rem;background:#238636;
       color:#fff;padding:.55rem 1rem;border-radius:var(--radius);
       font-size:.84rem;z-index:500;opacity:0;transition:opacity .22s;
       pointer-events:none;max-width:310px}
.toast.show{opacity:1}
.toast.err{background:var(--danger)}

/* ── Responsive ───────────────────────────────────────── */
@media(max-width:600px){
  .sidebar{display:none} .nav-tab span{display:none} .search{width:120px}
  .meta-grid{grid-template-columns:auto 1fr}
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
    <button class="nav-tab" data-tab="automations">⚡ <span>Automations</span></button>
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
    <table class="event-table">
      <thead><tr><th>Type</th><th>Name</th><th>Description</th></tr></thead>
      <tbody>
        <tr><td>Sensor</td><td><code>sensor.blink_downloader_status</code></td>
            <td>Total clips; attributes: session_downloads, used_mb, free_gb, last_download</td></tr>
        <tr><td>Event</td><td><code>blink_clip_downloaded</code></td>
            <td>Per-clip event: clip_id, camera, path, timestamp, size_bytes, duration, source</td></tr>
      </tbody>
    </table>

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
      </div>
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
  <div class="modal" style="max-width:420px">
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

<div class="toast" id="toast"></div>

<script>
'use strict';
// Ingress root prefix injected by server (empty for direct access, /api/hassio_ingress/TOKEN for ingress)
const _R = '__HAROOT__';

// ── Theme (light default; follows OS/browser prefers-color-scheme; manual override stored in localStorage) ─
(function(){
  const t = localStorage.getItem('blink_theme');
  if (t === 'dark') document.body.classList.add('dark');
  else if (t === 'light') document.body.classList.add('light');
  // If nothing stored, CSS media query handles it automatically.
})();
function _isDark() {
  if (document.body.classList.contains('dark')) return true;
  if (document.body.classList.contains('light')) return false;
  return window.matchMedia('(prefers-color-scheme:dark)').matches;
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
      el.innerHTML = `<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1">${c.camera}</span>`
        + `<span class="cam-badge">${c.total || 0}</span>`;
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
// Sync button icon when OS preference changes (e.g. system-wide dark mode toggle).
window.matchMedia('(prefers-color-scheme:dark)').addEventListener('change', updateThemeBtn);
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
    `<div class="sel-check">${selectedIds.has(c.id) ? '✓' : ''}</div>` +
    `</div>` +
    `<div class="clip-info">` +
    `<div class="clip-camera">${c.camera}</div>` +
    `<div class="clip-time">${fmtTs(c.timestamp)}</div>` +
    `<div class="clip-meta">` +
    (c.source ? `<span class="src-pill">${c.source}</span>` : '') +
    `<span>${fmtSize(c.size_bytes)}</span>` +
    (c.tags || []).map(t => `<span class="tag-pill">${t}</span>`).join('') +
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
      `<div>Camera</div><span>${c.camera}</span>` +
      `<div>Recorded</div><span>${fmtTs(c.timestamp)}</span>` +
      `<div>Duration</div><span>${fmtDur(c.duration) || '—'}</span>` +
      `<div>Size</div><span>${fmtSize(c.size_bytes) || '—'}</span>` +
      `<div>Source</div><span>${c.source || '—'}</span>` +
      `<div>Added</div><span>${fmtRelative(c.downloaded_at)}</span>`;
    updateStarBtn(c.starred);
    const dl = $('dl-link');
    dl.href = `${_R}/api/clips/${clipId}/stream`;
    dl.download = `${c.camera}_${(c.timestamp || '').replace(/[:.]/g, '-')}.mp4`;
    $('copy-path-btn').dataset.path = c.file_path || '';
    renderTags();

    $('modal-bg').classList.add('open');
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
    `<span class="tag-item">${t}<span class="rm" data-tag="${t}">×</span></span>`
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
  navClip(0) || closeModal();  // try to open same index or close
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

// Debounced filter listeners
let _dbt;
['search', 'date-range', 'starred-only', 'source-filter', 'tag-filter', 'sort-order'].forEach(id => {
  $(id).addEventListener(id === 'search' ? 'input' : 'change', () => {
    clearTimeout(_dbt); _dbt = setTimeout(() => { currentPage = 0; $('clip-grid').innerHTML = ''; loadClips(0); }, 380);
  });
});

// ── Status page ────────────────────────────────────────────────────────────
async function loadStatus() {
  const grid = $('status-grid');
  grid.innerHTML = '<div style="padding:2rem;color:var(--muted)">Loading…</div>';
  try {
    const [stats, cams, actData] = await Promise.all([
      api('/api/stats'), api('/api/cameras'), api('/api/activity?days=7'),
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

    // Cameras
    if (cams.length) {
      html += `<div class="status-card"><h3>📷 Cameras (${cams.length})</h3>`;
      cams.forEach(c => {
        html += `<div class="status-row"><span class="lbl">${c.camera}</span>`
          + `<span class="val">${c.total || 0} clips — ${c.today || 0} today</span></div>`;
      });
      html += `</div>`;
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
      <div class="act-bar-wrap" title="${total} clips" onclick="filterByDate('${date}')">
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
    if (_twofaState === 'needs_2fa') {
      overlay.classList.add('open');
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
      if (prev === 'needs_2fa' && _twofaState === 'connected') {
        overlay.classList.remove('open');
        toast('Signed in to Blink ✓');
        loadAll();
      } else if (_twofaState !== 'needs_2fa') {
        overlay.classList.remove('open');
      }
      _pendingSeq = 0;
    }
    if (_twofaState === 'error' && overlay.classList.contains('open')) {
      $('twofa-msg').textContent = s.message || 'Authentication failed.';
      $('twofa-msg').style.color = 'var(--danger)';
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
  await Promise.all([loadStats(), loadCameras(), loadClips(0)]);
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
    ) -> None:
        self._db = db
        self._download_path = download_path
        self._port = port
        self._trigger_download = trigger_download
        self._two_fa_callback = two_fa_callback
        self._auth_state_getter = auth_state_getter
        self._runner: web.AppRunner | None = None
        self.extra_status: dict = {}

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
        return app

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    async def _handle_index(self, request: web.Request) -> web.Response:
        # HA ingress sends X-Ingress-Path so the JS can prefix all API calls.
        # For direct port access the header is absent and the prefix is empty.
        ingress_path = request.headers.get("X-Ingress-Path", "").rstrip("/")
        html = _HTML.replace("'__HAROOT__'", f"'{ingress_path}'")
        return web.Response(text=html, content_type="text/html")

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_list_clips(self, request: web.Request) -> web.Response:
        q = request.rel_url.query
        try:
            limit = min(int(q.get("limit", 48)), 200)
            offset = int(q.get("offset", 0))
        except ValueError:
            limit, offset = 48, 0

        starred_raw = q.get("starred")
        starred = True if starred_raw == "1" else False if starred_raw == "0" else None

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
        )
        return web.json_response(clips)

    async def _handle_get_clip(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text="Clip not found")
        return web.json_response(clip)

    async def _handle_delete_clip(self, request: web.Request) -> web.Response:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text="Clip not found")
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
            raise web.HTTPNotFound(text="Clip not found")
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
            raise web.HTTPNotFound(text="Clip not found")
        return web.json_response({"id": clip_id, "tags": tags})

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        clip_id = request.match_info["id"]
        clip = await self._db.get_clip(clip_id)
        if not clip:
            raise web.HTTPNotFound(text="Clip not found")

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
            days = min(int(request.rel_url.query.get("days", 7)), 30)
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
