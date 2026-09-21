"""Moondream providers: the hosted Cloud API, and the on-device 0.5B model.

Both share ``_MoondreamDetectionMixin``. Moondream has no system-prompt
role and a much smaller context than the other providers, so instead of
one large assembled prompt these analyzers run Moondream's own
``/v1/detect`` and ``/v1/query`` endpoints per frame and fold the
resulting geometry into a short, augmented per-frame prompt — see the
mixin's docstring.

This is the module the ``_provider`` naming rule was learned on: as
``moondream.py`` its own ``import moondream`` resolved back to itself
under pyright wherever the optional GPU-only package is not installed.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from typing import Any

import aiohttp

from .base import (
    _API_TIMEOUT,
    _CONTENT_TYPE_JSON,
    _HEALTH_TIMEOUT,
    BaseAnalyzer,
    SecurityLayerSettings,
    _MostAlarming,
)

_LOGGER = logging.getLogger(__name__)


# Moondream Cloud pricing: (input_$/1M_tokens, output_$/1M_tokens)
# Source: https://docs.moondream.ai/pricing/
_MOONDREAM_CLOUD_PRICING: tuple[float, float] = (0.30, 2.50)


class _MoondreamDetectionMixin:
    """Shared detect-augmented reasoning helpers for Moondream analyzers.

    Both :class:`MoondreamCloudAnalyzer` and :class:`MoondreamLocalAnalyzer`
    run the same phased "detect objects, then reason about them" pipeline —
    only how each fetches a detection (HTTP vs. local package call) differs.
    This mixin holds the provider-agnostic pieces: bounding-box math and the
    natural-language hints built from it.
    """

    # Matches a plate/license-plate mention and its adjacent token(s), e.g.
    # "plate ABC1234", "license plate: XYZ-999", "plate # 7GHK123" —
    # case-insensitive, tolerant of an optional "license" prefix and a
    # colon/dash/# separator before the plate value itself. [A-Z] rather
    # than [A-Za-z] — re.IGNORECASE below already matches lowercase, so
    # spelling out both cases in the class is redundant (verified
    # behaviorally identical). The repeated group is atomic ((?>...), not
    # (?:...)) — each repetition's optional separator can never overlap
    # with its own mandatory alnum char, so there's no legitimate
    # alternate parse to backtrack into; this just makes that
    # non-ambiguity provable rather than merely true on inspection.
    # SonarQube's S8786 still flags the line below regardless — its
    # static check doesn't credit atomic groups as eliminating
    # backtracking, so the suppression below is warranted on top of the
    # atomic-group fix, not instead of it.
    _PLATE_MENTION_RE = re.compile(
        r"(?:license\s+)?plate\s*[:#-]?\s*[A-Z0-9](?>[ -]?[A-Z0-9]){1,10}",  # NOSONAR
        re.IGNORECASE,
    )

    @classmethod
    def _visual_detect_query(cls, car_description: str) -> str:
        """Return *car_description* with any license-plate mention removed.

        The protected-vehicle description is shown to the model verbatim in
        the text prompt (a plate number there is useful reasoning context —
        e.g. confirming a match when it happens to be legible). But this
        same string is also sent as a zero-shot ``/detect`` query to visually
        *locate* the vehicle's bounding box, and a plate number isn't a
        visual feature Moondream's detector can ground — including it can
        derail an otherwise simple "silver Kia Forte" query onto the wrong
        region, or onto nothing at all, corrupting the disambiguation this
        whole mechanism depends on. Falls back to the original description
        if stripping the plate mention would leave nothing usable.
        """
        stripped = cls._PLATE_MENTION_RE.sub("", car_description)
        stripped = re.sub(r"\s{2,}", " ", stripped)
        # Atomic for the same reason as _PLATE_MENTION_RE above — a comma
        # can't satisfy \s*, so there's nothing ambiguous to backtrack into.
        # Same SonarQube caveat as that pattern too (see its comment).
        stripped = re.sub(r"\s*(?>,\s*){2}", ", ", stripped).strip(" ,.-")  # NOSONAR
        return stripped or car_description

    @staticmethod
    def _bbox_gap(a: dict[str, float], b: dict[str, float]) -> float:
        """Return the Euclidean gap between two bounding boxes.

        0.0 means the boxes overlap; 1.0 means maximum separation.
        Uses normalised coordinates (0–1 relative to image width/height).
        """
        ax1, ay1 = a.get("x_min", 0.0), a.get("y_min", 0.0)
        ax2, ay2 = a.get("x_max", 1.0), a.get("y_max", 1.0)
        bx1, by1 = b.get("x_min", 0.0), b.get("y_min", 0.0)
        bx2, by2 = b.get("x_max", 1.0), b.get("y_max", 1.0)
        x_gap = max(0.0, max(ax1, bx1) - min(ax2, bx2))
        y_gap = max(0.0, max(ay1, by1) - min(ay2, by2))
        return (x_gap**2 + y_gap**2) ** 0.5

    @classmethod
    def _bbox_min_gap(
        cls,
        boxes_a: list[dict[str, float]],
        boxes_b: list[dict[str, float]],
    ) -> float:
        """Return the minimum gap between any pair of boxes across two lists."""
        min_gap = 1.0
        for a in boxes_a:
            for b in boxes_b:
                min_gap = min(min_gap, cls._bbox_gap(a, b))
        return min_gap

    @classmethod
    def _bbox_min_pairwise_gap(cls, boxes: list[dict[str, float]]) -> float:
        """Return the minimum gap between any two distinct boxes in one list.

        Used to detect a second vehicle parked or stopped close to the
        protected vehicle when no person or animal is in frame — e.g. two
        detected "car" boxes sitting right next to each other.
        """
        if len(boxes) < 2:
            return 1.0
        min_gap = 1.0
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                min_gap = min(min_gap, cls._bbox_gap(boxes[i], boxes[j]))
        return min_gap

    @staticmethod
    def _bbox_iou(a: dict[str, float], b: dict[str, float]) -> float:
        """Return the Intersection-over-Union overlap ratio of two boxes.

        Unlike :meth:`_bbox_gap` (which measures separation and is 0.0 for
        both "identical box" and "merely touching, no area in common"), IoU
        distinguishes those two cases — needed to tell "the same physical
        vehicle detected twice" (high IoU) apart from "two different
        vehicles parked flush against each other" (near-zero IoU despite a
        zero gap).
        """
        ax1, ay1 = a.get("x_min", 0.0), a.get("y_min", 0.0)
        ax2, ay2 = a.get("x_max", 1.0), a.get("y_max", 1.0)
        bx1, by1 = b.get("x_min", 0.0), b.get("y_min", 0.0)
        bx2, by2 = b.get("x_max", 1.0), b.get("y_max", 1.0)
        inter_w = max(0.0, min(ax2, bx2) - max(ax1, bx1))
        inter_h = max(0.0, min(ay2, by2) - max(ay1, by1))
        intersection = inter_w * inter_h
        area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
        area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
        union = area_a + area_b - intersection
        if union <= 0.0:
            return 0.0
        return intersection / union

    @classmethod
    def _dedupe_boxes(
        cls, boxes: list[dict[str, float]], iou_threshold: float = 0.3
    ) -> list[dict[str, float]]:
        """Collapse duplicate detections of the same physical object.

        Zero-shot open-vocabulary detectors like Moondream's ``/detect`` can
        return more than one box for a single physical object — e.g. a
        generic "car" query matching both the vehicle's full body and a
        tighter crop of the same vehicle as two separate boxes. Left
        undeduplicated, two boxes for one parked car get treated by
        :meth:`_other_vehicle_boxes` as two distinct vehicles, producing a
        false "another vehicle stopped right next to the protected vehicle"
        alert for a car that is simply parked alone in its own driveway.
        Boxes with IoU at or above *iou_threshold* are treated as the same
        object; the larger (by area) box of each overlapping pair suppresses
        the smaller one. Surviving boxes are returned in their original
        input order — only overlap decides what's removed, never reordering
        the untouched majority of non-overlapping boxes.
        """
        if len(boxes) <= 1:
            return boxes

        def area(b: dict[str, float]) -> float:
            return max(0.0, b.get("x_max", 1.0) - b.get("x_min", 0.0)) * max(
                0.0, b.get("y_max", 1.0) - b.get("y_min", 0.0)
            )

        priority = sorted(range(len(boxes)), key=lambda i: area(boxes[i]), reverse=True)
        suppressed: set[int] = set()
        for pos, i in enumerate(priority):
            if i in suppressed:
                continue
            for j in priority[pos + 1 :]:
                if (
                    j not in suppressed
                    and cls._bbox_iou(boxes[i], boxes[j]) >= iou_threshold
                ):
                    suppressed.add(j)
        return [b for idx, b in enumerate(boxes) if idx not in suppressed]

    @classmethod
    def _other_vehicle_boxes(
        cls,
        protected_boxes: list[dict[str, float]],
        all_boxes: list[dict[str, float]],
    ) -> list[dict[str, float]]:
        """Return boxes from *all_boxes* that are not the protected vehicle.

        A box counts as "the protected vehicle" when it substantially
        overlaps (IoU ≥ 0.5) one of *protected_boxes* — the generic "car"
        detect and the description-specific detect each produce their own
        box for the same physical vehicle, with minor jitter, so this
        cross-matches them by overlap rather than identity. Returns
        *all_boxes* unchanged when *protected_boxes* is empty, since there's
        nothing yet to distinguish "other" from.
        """
        if not protected_boxes:
            return all_boxes
        return [
            box
            for box in all_boxes
            if max(cls._bbox_iou(box, p) for p in protected_boxes) < 0.5
        ]

    @staticmethod
    def _proximity_hint(gap: float, subject: str) -> str:
        """Build an [INTERNAL PROXIMITY HINT] string tiering *subject*'s
        distance from the protected vehicle from touching down to several
        feet away. Used for person/animal proximity, where the subject
        shares the vehicle's ground plane and 2D bounding-box distance is a
        reasonable proxy for real-world distance. Vehicle-to-vehicle
        proximity uses :meth:`_vehicle_proximity_hint` instead — see its
        docstring for why the two cases need different calibration.
        """
        prefix = (
            "[INTERNAL PROXIMITY HINT — use for reasoning only, "
            "do NOT copy this text into the description]: "
        )
        if gap <= 0.0:
            return (
                f"{prefix}The {subject} appears to be directly touching or pressed "
                "against the vehicle. Describe this as 'right next to' "
                "or 'touching the car' in plain English."
            )
        if gap < 0.05:
            return (
                f"{prefix}The {subject} appears to be less than 1 foot from the vehicle. "
                "Describe this as 'very close to the car' in plain English."
            )
        if gap < 0.15:
            return (
                f"{prefix}The {subject} appears to be roughly 1–3 feet from the vehicle. "
                "This is close but not touching — do NOT flag as suspicious "
                "unless actively lingering, reaching for, or touching the car. "
                "Describe this as 'a couple of feet from the car' in plain English."
            )
        return (
            f"{prefix}The {subject} appears to be several feet from the vehicle — "
            "this distance is NOT suspicious on its own. "
            "Set suspicious=false unless there is other clear evidence of tampering. "
            "Describe this as 'well away from the car' in plain English."
        )

    @staticmethod
    def _vehicle_proximity_hint(gap: float) -> str:
        """Build an [INTERNAL VEHICLE PROXIMITY HINT] for another vehicle
        detected near the protected vehicle.

        Unlike a person or animal (which stands on the ground right next to
        the car, so 2D bounding-box distance tracks real-world distance
        reasonably well), two vehicle boxes can appear close or even
        touching in a 2D frame while being many feet apart in real depth —
        a car driving past on the street behind or beside a parked car
        routinely overlaps it in screen space purely from camera
        perspective. More fundamentally, even a vehicle that genuinely does
        park or stop close to the protected one — a second household car, a
        visitor, a neighbor — is routine and not a security concern by
        itself; only a person or animal actually near the vehicle makes a
        scene worth flagging (see :meth:`_proximity_hint`). This hint is
        therefore unconditional rather than gap-dependent: it never asks the
        model to weigh the bounding-box distance, only to describe the scene
        accurately and leave suspicious=false. The analyzer also enforces
        this in code (see the ``multiple_vehicles``-and-no-subjects override
        in ``_call_model``), so an instruction-following slip here — small
        vision models don't reliably honor negative constraints — can't by
        itself produce a false alert.
        """
        return (
            "[INTERNAL VEHICLE PROXIMITY HINT — use for reasoning only, do "
            "NOT copy this text into the description]: Another vehicle's "
            "bounding box is close to or overlapping the protected "
            f"vehicle's in this single 2D frame (estimated gap {gap:.2f}). "
            "This overlap can happen purely from camera perspective — a car "
            "driving past on the street behind or beside the parked vehicle "
            "commonly appears adjacent to or touching it in the frame while "
            "actually being many feet away in real distance. Even when the "
            "other vehicle genuinely is parked or stopped close by, that is "
            "routine (a second household car, a visitor, a neighbor) and "
            "NOT suspicious on its own — set suspicious=false regardless of "
            "how close the vehicles appear or whether one is stopping, "
            "parking, or backing up. Describe the scene plainly, e.g. 'a "
            "second car is parked near the protected vehicle' or 'a car "
            "drove up the street', without alarm language. Only set "
            "suspicious=true if a person or animal is also visible actually "
            "touching, lingering near, or reaching toward either vehicle."
        )

    @staticmethod
    def _position_hint(subjects: list[tuple[str, dict[str, float]]]) -> str:
        """Build an [INTERNAL POSITION HINT] locating up to 3 detected
        subjects within the frame, for cameras with no protected-vehicle
        proximity rules in play. Each entry is a ``(label, box)`` pair, e.g.
        ``("Person", box)``, ``("Animal", box)``, or ``("Vehicle", box)`` —
        the label lets one hint cover people, animals, and incidental
        vehicles on cameras that aren't watching a protected vehicle.
        Returns "" when *subjects* is empty.
        """
        position_notes = []
        label_counts: dict[str, int] = {}
        for label, box in subjects[:3]:
            label_counts[label] = label_counts.get(label, 0) + 1
            cx = (box.get("x_min", 0.0) + box.get("x_max", 1.0)) / 2
            cy = (box.get("y_min", 0.0) + box.get("y_max", 1.0)) / 2
            if cx < 0.33:
                side = "left"
            elif cx > 0.67:
                side = "right"
            else:
                side = "centre"
            if cy < 0.33:
                vert = "top"
            elif cy > 0.67:
                vert = "bottom"
            else:
                vert = "middle"
            position_notes.append(
                f"{label} {label_counts[label]} is in the {vert}-{side} of the frame"
            )
        if not position_notes:
            return ""
        return (
            "[INTERNAL POSITION HINT — use for reasoning only, "
            "do NOT copy this text into the description]: "
            + "; ".join(position_notes)
            + "."
        )

    @staticmethod
    def _vehicle_hint() -> str:
        """Build an [INTERNAL VEHICLE HINT] for cameras with no protected-
        vehicle rules in play, when a vehicle is detected with no person
        present. Nudges the model to describe ordinary passing or parking
        traffic plainly — e.g. "a car drove up the street" — instead of
        reflexively treating any vehicle in frame as noteworthy just
        because it appeared on a motion-triggered clip.
        """
        return (
            "[INTERNAL VEHICLE HINT — use for reasoning only, do NOT copy "
            "this text into the description]: A vehicle is visible in this "
            "frame. If it is simply passing through, driving by, or parking "
            "normally with no person involved, describe it plainly (e.g. 'a "
            "car drove up the street') and set suspicious=false unless it is "
            "clearly behaving abnormally (e.g. stopping to case the property)."
        )

    def _augment_noncar_prompt(
        self,
        prompt: str,
        persons: list[dict[str, float]],
        animals: list[dict[str, float]],
        generic_vehicles: list[dict[str, float]],
    ) -> str:
        """Non-car camera: inject subject positions (person, animal,
        incidental vehicle) to help the model describe the scene accurately
        without borrowing protected-vehicle rules."""
        augmented_prompt = prompt
        labeled_subjects = (
            [("Person", p) for p in persons]
            + [("Animal", a) for a in animals]
            + [("Vehicle", v) for v in generic_vehicles]
        )
        position_hint = self._position_hint(labeled_subjects)
        if position_hint:
            augmented_prompt += f"\n\n{position_hint}"
        if generic_vehicles and not persons:
            augmented_prompt += f"\n\n{self._vehicle_hint()}"
        return augmented_prompt

    @staticmethod
    def _no_subject_response() -> str:
        """Hardcoded JSON result for a frame with no person, animal, or second
        vehicle detected — skips the expensive query/caption calls entirely.
        """
        return (
            '{"suspicious": false, "confidence": 0.9, '
            '"description": "No person detected in this frame. '
            'Motion likely caused by a vehicle, animal, or environmental factor."}'
        )

    @staticmethod
    def _force_not_suspicious(response: str) -> str:
        """Rewrite a raw model JSON response to force ``suspicious: false``.

        Called when detection evidence shows the only thing that could have
        driven a "suspicious" verdict for this frame was vehicle-to-vehicle
        proximity with no person or animal present (see
        :meth:`_vehicle_proximity_hint`). That hint asks the model to always
        answer suspicious=false in this case, but small vision-language
        models don't reliably follow negative instructions, so this enforces
        the policy in code instead of trusting the model's own fields.
        Returns *response* unchanged if it isn't parseable JSON or is already
        not suspicious.
        """
        start = response.find("{")
        end = response.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return response
        try:
            obj = json.loads(response[start : end + 1])
        except json.JSONDecodeError:
            return response
        if not obj.get("suspicious"):
            return response
        obj["suspicious"] = False
        try:
            obj["confidence"] = min(0.3, float(obj.get("confidence", 0.0) or 0.0))
        except (TypeError, ValueError):
            obj["confidence"] = 0.0
        return json.dumps(obj)


class MoondreamCloudAnalyzer(_MoondreamDetectionMixin, BaseAnalyzer):
    """Analyzes clips via the Moondream Cloud API (api.moondream.ai).

    Pass ``finetune_model`` (e.g. ``"moondream3-preview/abc123@50"``) to use a
    fine-tuned checkpoint for inference instead of the base model.  Build the
    model ID with :meth:`MoondreamFineTuneManager.get_model_id` after training.
    """

    _BASE_URL = "https://api.moondream.ai/v1"
    # Model identifier returned by the Moondream API as of mid-2025.
    _MODEL_ID = "moondream3-preview"

    def __init__(
        self,
        api_key: str,
        prompt: str,
        car_description: str = "",
        max_frames: int = 3,
        frame_interval: float = 2.0,
        suspicious_keywords: list[str] | None = None,
        camera_prompts: dict[str, str] | None = None,
        camera_descriptions: dict[str, str] | None = None,
        frame_strategy: str = "smart",
        car_cameras: list[str] | None = None,
        car_zones: dict[str, dict[str, Any]] | None = None,
        finetune_model: str = "",
        security_settings: SecurityLayerSettings | None = None,
    ) -> None:
        super().__init__(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
            security_settings=security_settings,
        )
        self._api_key = api_key
        self._finetune_model = finetune_model
        self._session: aiohttp.ClientSession | None = None

    @property
    def provider_name(self) -> str:
        return "moondream_cloud"

    def model_name(self) -> str:
        return self._finetune_model or self._MODEL_ID

    def set_finetune_model(self, model_id: str) -> None:
        """Switch inference to a fine-tuned checkpoint at runtime, no restart.

        *model_id* is the value returned by
        :meth:`MoondreamFineTuneManager.get_model_id` (e.g.
        ``"moondream3-preview/abc123@50"``). Pass an empty string to revert
        to the base model. Mirrors the hot-swap pattern used by
        ``update_camera_descriptions``/``update_camera_prompts`` elsewhere on
        :class:`BaseAnalyzer`.
        """
        self._finetune_model = model_id

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def health_check(self) -> bool:
        """Return True if the API key is set and the cloud endpoint is reachable.

        Cached for ``_HEALTH_CHECK_CACHE_SECONDS`` since this hits Moondream
        Cloud's real API — see the cache helpers on :class:`BaseAnalyzer` for
        why.
        """
        cached = self._cached_health_check_result()
        if cached is not None:
            return cached
        if not self._api_key:
            _LOGGER.warning("Moondream Cloud: no API key configured")
            return self._store_health_check_result(False)
        try:
            session = self._get_session()
            async with session.get(
                "https://api.moondream.ai/",
                timeout=_HEALTH_TIMEOUT,
                allow_redirects=True,
            ) as resp:
                return self._store_health_check_result(resp.status < 500)
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.debug("Moondream Cloud health check failed: %s", exc)
            return self._store_health_check_result(False)

    def model_pricing(self) -> tuple[float, float]:
        """Return (input_price, output_price) per 1M tokens for Moondream Cloud."""
        return _MOONDREAM_CLOUD_PRICING

    async def fetch_models(self) -> list[dict[str, Any]]:
        inp, out = _MOONDREAM_CLOUD_PRICING
        models: list[dict[str, Any]] = [
            {
                "name": self._MODEL_ID,
                "display_name": f"Moondream Cloud (${inp:.2f}/${out:.2f} per 1M tokens)",
                "description": f"Moondream Cloud API (${inp:.2f}/${out:.2f} per 1M tokens)",
            }
        ]
        if self._finetune_model:
            models.append(
                {
                    "name": self._finetune_model,
                    "display_name": f"Moondream Fine-tuned: {self._finetune_model}",
                    "description": "Custom fine-tuned model via Moondream Cloud fine-tuning API",
                }
            )
        return models

    # Image encoder cost per 640px JPEG frame (empirical, based on observed
    # Moondream Cloud usage: ~869 tokens for a single-frame request with a
    # short prompt, leaving ~800 tokens for the image after subtracting text).
    # The Moondream API does not return usage stats, so these are estimates.
    _IMAGE_TOKENS_PER_FRAME: int = 800

    async def _detect_objects(
        self, frame: bytes, object_name: str
    ) -> list[dict[str, float]]:
        """Call the Moondream /detect endpoint for ``object_name``.

        Returns a list of normalised bounding boxes
        ``[{x_min, y_min, x_max, y_max}]``.  Returns ``[]`` on any error so
        callers can safely skip detect-based logic when it fails.
        """
        image_b64 = base64.b64encode(frame).decode("ascii")
        payload: dict[str, Any] = {
            "image_url": f"data:image/jpeg;base64,{image_b64}",
            "object": object_name,
        }
        if self._finetune_model:
            payload["model"] = self._finetune_model
        headers = {
            "X-Moondream-Auth": self._api_key,
            "Content-Type": _CONTENT_TYPE_JSON,
        }
        try:
            session = self._get_session()
            async with session.post(
                f"{self._BASE_URL}/detect",
                json=payload,
                headers=headers,
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug(
                        "Moondream /detect returned HTTP %d for %r",
                        resp.status,
                        object_name,
                    )
                    return []
                data = await resp.json()
                objects = data.get("objects", [])
                return [o for o in objects if isinstance(o, dict)]
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.debug("Moondream /detect failed for %r: %s", object_name, exc)
            return []

    async def _caption_frame(self, frame: bytes) -> str:
        """Call Moondream Cloud's dedicated ``/caption`` endpoint for a
        factual, grounding description of *frame*.

        The caption skill is tuned specifically for scene description
        (elements, context, colors, positioning) rather than free-form Q&A,
        so injecting its output into the ``/query`` prompt as grounding
        context measurably reduces hallucination in the final structured
        description compared to relying on ``/query`` alone. Uses
        ``length="short"`` rather than ``"normal"`` — a short caption
        grounds the query with the notable subject and its immediate
        surroundings in one concise sentence instead of an exhaustive,
        multi-sentence inventory of everything in frame (background
        vehicles, foliage, utility poles, etc.), which was leaking into the
        final description and driving up completion tokens for little
        security value. Returns "" on any error so callers can skip the
        grounding hint.
        """
        image_b64 = base64.b64encode(frame).decode("ascii")
        payload: dict[str, Any] = {
            "image_url": f"data:image/jpeg;base64,{image_b64}",
            "length": "short",
            "stream": False,
        }
        if self._finetune_model:
            payload["model"] = self._finetune_model
        headers = {
            "X-Moondream-Auth": self._api_key,
            "Content-Type": _CONTENT_TYPE_JSON,
        }
        try:
            session = self._get_session()
            async with session.post(
                f"{self._BASE_URL}/caption",
                json=payload,
                headers=headers,
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status != 200:
                    _LOGGER.debug("Moondream /caption returned HTTP %d", resp.status)
                    return ""
                data = await resp.json()
                caption = str(data.get("caption", ""))
                self._last_prompt_tokens += self._IMAGE_TOKENS_PER_FRAME
                self._last_completion_tokens += max(1, len(caption) // 4)
                return caption
        except TimeoutError:
            _LOGGER.debug("Moondream /caption request timed out")
            return ""
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.debug("Moondream /caption request failed: %s", exc)
            return ""

    async def _detect_protected_vehicle(
        self, frame: bytes, all_car_boxes: list[dict[str, float]]
    ) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
        """Disambiguate the protected vehicle from other cars in *frame*.

        Moondream's ``/detect`` accepts free-text zero-shot object queries,
        so when more than one "car" box is present, the protected vehicle's
        own description (e.g. "silver Kia Forte") is used as a second,
        targeted detect query instead of treating every car box the same —
        this distinguishes the actual protected vehicle from a visitor's
        car, a passing vehicle, or a second car parked nearby. Skipped
        entirely when there's only 0-1 car boxes, since there's nothing to
        disambiguate. Falls back to treating all boxes as the protected
        vehicle (no "other" vehicles) if the description-specific detect
        finds nothing usable — an unusually worded description Moondream
        can't match should not manufacture a false "other vehicle" alert.

        Returns ``(protected_vehicle_boxes, other_vehicle_boxes)``.
        """
        all_car_boxes = self._dedupe_boxes(all_car_boxes)
        if len(all_car_boxes) <= 1 or not self._car_description:
            return all_car_boxes, []

        protected_boxes = await self._detect_objects(
            frame, self._visual_detect_query(self._car_description)
        )
        await asyncio.sleep(0.55)
        if not protected_boxes:
            return all_car_boxes, []

        other_boxes = self._other_vehicle_boxes(protected_boxes, all_car_boxes)
        return protected_boxes, other_boxes

    async def _call_api_frame(self, frame: bytes, prompt: str) -> str:
        """Send a single JPEG frame to the Moondream Cloud /query endpoint.

        Reasoning mode is always enabled — it adds 10-20 % latency but
        substantially improves multi-step spatial analysis (proximity
        estimates, evasive behaviour detection) with no extra cost.

        Token counts are not returned by the Moondream API; we accumulate
        estimates in ``_last_prompt_tokens`` / ``_last_completion_tokens``
        so the usage table shows approximate figures instead of N/A.
        """
        image_b64 = base64.b64encode(frame).decode("ascii")
        payload: dict[str, Any] = {
            "image_url": f"data:image/jpeg;base64,{image_b64}",
            "question": prompt,
            "stream": False,
            "reasoning": True,
        }
        if self._finetune_model:
            payload["model"] = self._finetune_model
        headers = {
            "X-Moondream-Auth": self._api_key,
            "Content-Type": _CONTENT_TYPE_JSON,
        }
        try:
            session = self._get_session()
            async with session.post(
                f"{self._BASE_URL}/query",
                json=payload,
                headers=headers,
                timeout=_API_TIMEOUT,
            ) as resp:
                if resp.status == 429:
                    self._last_rate_limited = True
                    _LOGGER.warning("Moondream Cloud: rate limit hit")
                    return ""
                if resp.status == 401:
                    self._last_transient_error = False
                    _LOGGER.error("Moondream Cloud: invalid API key (HTTP 401)")
                    return ""
                if resp.status != 200:
                    _LOGGER.warning("Moondream Cloud returned HTTP %d", resp.status)
                    return ""
                data = await resp.json()
                answer = str(data.get("answer", ""))
                # Accumulate estimated token counts across frames.
                # Prompt: image encoder tokens + text tokens (4 chars ≈ 1 token).
                self._last_prompt_tokens += self._IMAGE_TOKENS_PER_FRAME + max(
                    1, len(prompt) // 4
                )
                # Completion: output text tokens.
                self._last_completion_tokens += max(1, len(answer) // 4)
                return answer
        except TimeoutError:
            _LOGGER.warning("Moondream Cloud request timed out")
            return ""
        except (aiohttp.ClientError, OSError) as exc:
            _LOGGER.warning("Moondream Cloud request failed: %s", exc)
            return ""

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        """Analyse frames via Moondream Cloud with detect-augmented analysis.

        For each frame:
        1. Run ``/detect`` for "person". A frame with no person still gets
           checked for an animal or a vehicle before being written off —
           every camera's job is to describe what's actually happening, not
           just track people, and a car camera additionally needs to catch
           anyone or anything getting close to the protected vehicle.
        2. If nothing of interest was found, skip the expensive ``/caption``
           and ``/query`` calls and record a clear result for that frame.
        3. For car cameras: also run ``/detect`` for "car". When more than
           one car box is found, a second targeted detect using the
           protected vehicle's own description disambiguates it from any
           other vehicle in frame (see :meth:`_detect_protected_vehicle`),
           and precise bounding-box proximity data is injected into the
           query prompt so the model can make an evidence-based
           suspicious/clear decision. Non-car cameras instead run a plain
           "vehicle" detect — there's no specific vehicle to disambiguate,
           just an incidental one worth captioning accurately.
        4. Run ``/caption`` once on the frame and inject its factual scene
           description as grounding context — a dedicated captioning call
           tends to describe *what's actually there* more reliably than
           relying solely on the free-form ``/query`` answer, which reduces
           hallucination in the final structured description.
        5. Run ``/query`` (with reasoning=True) on the augmented prompt and
           pick the most alarming result across all frames.

        Respects the 2 req/s rate limit with a 0.55 s delay between requests.
        """
        if not frames:
            return ""

        camera = getattr(self, "_current_camera", "")
        car_applies = self._car_protection_applies(camera)
        best = _MostAlarming()

        for i, frame in enumerate(frames):
            kind, resp = await self._analyze_one_moondream_frame(
                frame, prompt, camera, car_applies
            )
            if kind == "no_subject":
                best.offer_unranked(self._no_subject_response())
            elif kind == "result":
                suspicious, confidence, description = self._try_parse_json(resp)
                if description:
                    best.offer(suspicious, confidence, resp)
                else:
                    best.offer_unranked(resp)
            # kind == "no_response": nothing to do, this frame is skipped.

            if i != len(frames) - 1:
                await asyncio.sleep(0.55)

        return best.response

    async def _analyze_one_moondream_frame(
        self, frame: bytes, prompt: str, camera: str, car_applies: bool
    ) -> tuple[str, str]:
        """Run detect/caption/query for a single frame.

        Returns ``("no_subject", "")`` when nothing of interest was found,
        ``("no_response", "")`` when the query call itself failed, or
        ``("result", resp)`` with the raw query response otherwise.
        """
        (
            persons,
            animals,
            protected_boxes,
            other_vehicle_boxes,
            generic_vehicles,
        ) = await self._detect_frame_subjects(frame, car_applies)
        multiple_vehicles = car_applies and bool(other_vehicle_boxes)

        if (
            not persons
            and not animals
            and not multiple_vehicles
            and not generic_vehicles
        ):
            # Nothing of interest in this frame — motion likely caused by
            # something else. Record a clear result and move on without
            # spending query tokens.
            return "no_subject", ""

        augmented_prompt, multiple_vehicles = await self._augment_prompt_for_frame(
            frame,
            prompt,
            camera,
            car_applies,
            persons,
            animals,
            protected_boxes,
            other_vehicle_boxes,
            generic_vehicles,
            multiple_vehicles,
        )

        # ── Phase 2b: caption grounding ───────────────────────────────
        caption = await self._caption_frame(frame)
        await asyncio.sleep(0.55)
        if caption:
            augmented_prompt += (
                "\n\n[INTERNAL SCENE CAPTION — factual grounding only, do "
                f"NOT copy this text verbatim into your description]: {caption}"
            )

        # ── Phase 3: full query with augmented prompt ────────────────
        resp = await self._call_api_frame(frame, augmented_prompt)
        if not resp:
            return "no_response", ""

        subjects = persons + animals
        if multiple_vehicles and not subjects:
            resp = self._force_not_suspicious(resp)

        return "result", resp

    async def _detect_frame_subjects(
        self, frame: bytes, car_applies: bool
    ) -> tuple[
        list[dict[str, float]],
        list[dict[str, float]],
        list[dict[str, float]],
        list[dict[str, float]],
        list[dict[str, float]],
    ]:
        """Detect persons and, if none found, animals and vehicles too.

        A frame with no person still gets checked for an animal or a
        vehicle before being written off — otherwise a dog sniffing at the
        car, another car pulling up tight beside it, or a car simply
        driving past a non-car camera would be silently reduced to a
        generic "no person detected" result with no caption at all.

        Returns ``(persons, animals, protected_boxes, other_vehicle_boxes,
        generic_vehicles)``.
        """
        persons = await self._detect_objects(frame, "person")
        await asyncio.sleep(0.55)

        animals: list[dict[str, float]] = []
        protected_boxes: list[dict[str, float]] = []
        other_vehicle_boxes: list[dict[str, float]] = []
        generic_vehicles: list[dict[str, float]] = []
        if not persons:
            animals = await self._detect_objects(frame, "animal")
            await asyncio.sleep(0.55)
            if car_applies:
                all_car_boxes = await self._detect_objects(frame, "car")
                await asyncio.sleep(0.55)
                (
                    protected_boxes,
                    other_vehicle_boxes,
                ) = await self._detect_protected_vehicle(frame, all_car_boxes)
            else:
                generic_vehicles = await self._detect_objects(frame, "vehicle")
                await asyncio.sleep(0.55)

        return persons, animals, protected_boxes, other_vehicle_boxes, generic_vehicles

    async def _augment_prompt_for_frame(
        self,
        frame: bytes,
        prompt: str,
        camera: str,
        car_applies: bool,
        persons: list[dict[str, float]],
        animals: list[dict[str, float]],
        protected_boxes: list[dict[str, float]],
        other_vehicle_boxes: list[dict[str, float]],
        generic_vehicles: list[dict[str, float]],
        multiple_vehicles: bool,
    ) -> tuple[str, bool]:
        """Augment *prompt* with spatial/proximity context for this frame.

        Returns ``(augmented_prompt, multiple_vehicles)`` — multiple_vehicles
        is recomputed here because the car_applies branch may re-run car
        detection when Phase 1 skipped it (a person was present).
        """
        subjects = persons + animals

        if car_applies:
            return await self._augment_car_prompt(
                frame,
                prompt,
                camera,
                subjects,
                protected_boxes,
                other_vehicle_boxes,
                multiple_vehicles,
            )

        return (
            self._augment_noncar_prompt(prompt, persons, animals, generic_vehicles),
            multiple_vehicles,
        )

    async def _augment_car_prompt(
        self,
        frame: bytes,
        prompt: str,
        camera: str,
        subjects: list[dict[str, float]],
        protected_boxes: list[dict[str, float]],
        other_vehicle_boxes: list[dict[str, float]],
        multiple_vehicles: bool,
    ) -> tuple[str, bool]:
        augmented_prompt = prompt

        if not protected_boxes and not other_vehicle_boxes:
            all_car_boxes = await self._detect_objects(frame, "car")
            await asyncio.sleep(0.55)
            (
                protected_boxes,
                other_vehicle_boxes,
            ) = await self._detect_protected_vehicle(frame, all_car_boxes)
            multiple_vehicles = bool(other_vehicle_boxes)

        if subjects and (protected_boxes or other_vehicle_boxes):
            # Gap is measured against EVERY detected car box, not just
            # the one disambiguation labelled "protected" — two
            # independent zero-shot detect calls (generic "car" vs.
            # the description-specific query) can each draw a
            # slightly different box for the SAME physical vehicle,
            # and a person leaning on/touching the car shifts that
            # box enough to push its IoU with the earlier "protected"
            # box below the match threshold. That misclassifies the
            # real vehicle as "other" at the exact moment contact is
            # happening — the one moment this hint must not miss.
            # Identity of which box is "the protected one" only
            # matters for the vehicle-vs-vehicle case below.
            gap = self._bbox_min_gap(subjects, protected_boxes + other_vehicle_boxes)
            augmented_prompt += f"\n\n{self._proximity_hint(gap, 'person or animal')}"
        elif multiple_vehicles:
            gap = (
                self._bbox_min_gap(protected_boxes, other_vehicle_boxes)
                if protected_boxes
                else self._bbox_min_pairwise_gap(other_vehicle_boxes)
            )
            augmented_prompt += f"\n\n{self._vehicle_proximity_hint(gap)}"
        elif subjects and self._car_zones.get(camera):
            # Car detect found no car box at all this frame (e.g. the
            # vehicle is partly out of view or detect simply missed
            # it), but a fixed car zone is configured for this
            # camera — use it as a fallback proximity reference so a
            # person standing where the car normally is still gets
            # flagged instead of silently falling through with no
            # hint at all. _car_zone_bbox reduces either a rectangle or a
            # freeform polygon zone to the plain x_min/y_min/x_max/y_max
            # shape _bbox_min_gap expects.
            gap = self._bbox_min_gap(
                subjects, [self._car_zone_bbox(self._car_zones[camera])]
            )
            augmented_prompt += f"\n\n{self._proximity_hint(gap, 'person or animal')}"
        # else: car detect returned nothing and no zone is configured —
        # no proximity hint; the base prompt's vehicle-distance rules
        # still apply if the model can see the car in the frames.
        # Explicit suppression here caused missed alerts when detect
        # failed despite the car being visibly in frame.

        return augmented_prompt, multiple_vehicles


# ---------------------------------------------------------------------------
# Moondream local provider (0.5B INT8, runs on-device)
# ---------------------------------------------------------------------------


class MoondreamLocalAnalyzer(_MoondreamDetectionMixin, BaseAnalyzer):
    """Runs the Moondream 0.5B INT8 model locally using the moondream package.

    The model (~430 MB) is downloaded from Moondream's servers on the first
    run and cached in the default moondream cache directory.  Subsequent
    starts reuse the cached file.  Inference runs in a thread executor so the
    asyncio event loop is never blocked.

    Mirrors :class:`MoondreamCloudAnalyzer`'s detect-augmented pipeline
    (person/animal/vehicle detection feeding proximity hints, plus a
    caption-grounded query) using the local package's ``detect``/``caption``/
    ``query`` methods instead of HTTP calls — see
    :meth:`_analyze_frame_sync` for the per-frame pipeline.
    """

    _MODEL_ID = "moondream-0_5b-int8"

    def __init__(
        self,
        prompt: str,
        car_description: str = "",
        max_frames: int = 3,
        frame_interval: float = 2.0,
        suspicious_keywords: list[str] | None = None,
        camera_prompts: dict[str, str] | None = None,
        camera_descriptions: dict[str, str] | None = None,
        frame_strategy: str = "smart",
        car_cameras: list[str] | None = None,
        car_zones: dict[str, dict[str, Any]] | None = None,
        security_settings: SecurityLayerSettings | None = None,
    ) -> None:
        super().__init__(
            prompt=prompt,
            car_description=car_description,
            max_frames=max_frames,
            frame_interval=frame_interval,
            suspicious_keywords=suspicious_keywords,
            camera_prompts=camera_prompts,
            camera_descriptions=camera_descriptions,
            frame_strategy=frame_strategy,
            car_cameras=car_cameras,
            car_zones=car_zones,
            security_settings=security_settings,
        )
        self._md_model: Any = None
        self._model_lock: asyncio.Lock | None = None
        self._model_ready = False

    def _get_lock(self) -> asyncio.Lock:
        if self._model_lock is None:
            self._model_lock = asyncio.Lock()
        return self._model_lock

    @property
    def provider_name(self) -> str:
        return "moondream_local"

    def model_name(self) -> str:
        return self._MODEL_ID

    async def close(self) -> None:
        self._md_model = None
        self._model_ready = False

    def _load_model_sync(self) -> None:
        """Load the Moondream model for on-device inference (blocking — run in executor).

        ``local=True`` is passed explicitly and is load-bearing: the
        ``moondream`` package silently falls back to Moondream *Cloud* when
        ``local`` is omitted, which would send every camera frame off-device
        to api.moondream.ai — with no API key configured, unauthenticated —
        while this provider is meant to be fully on-device. On-device
        inference requires a CUDA or Apple Silicon GPU (via the package's
        "Photon" engine); hosts without one raise cleanly here instead of
        silently degrading into a cloud leak, and ``_ensure_model`` reports
        the provider as unavailable rather than a false "ready".
        """
        import moondream as md  # type: ignore[import-not-found]

        _LOGGER.info("Loading Moondream local model '%s'", self._MODEL_ID)
        self._md_model = md.vl(model=self._MODEL_ID, local=True)
        self._model_ready = True
        _LOGGER.info("Moondream local model ready")

    async def _ensure_model(self) -> bool:
        """Ensure the model is loaded. Returns True when ready."""
        if self._model_ready:
            return True
        lock = self._get_lock()
        async with lock:
            if self._model_ready:
                return True
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self._load_model_sync)
                return True
            except ImportError:
                _LOGGER.exception(
                    "moondream package (or a local-inference dependency it "
                    "requires, such as kestrel or torch) is not installed. "
                    "Install it with: pip install moondream"
                )
                return False
            except Exception:
                _LOGGER.exception(
                    "Failed to load Moondream local model. On-device "
                    "Moondream inference requires a CUDA or Apple Silicon GPU — "
                    "if this host doesn't have one, use moondream_cloud or "
                    "ollama instead."
                )
                return False

    async def health_check(self) -> bool:
        """Return True once the model is loaded (triggers download on first call)."""
        return await self._ensure_model()

    async def fetch_models(self) -> list[dict[str, Any]]:
        return [
            {
                "name": self._MODEL_ID,
                "description": "Moondream 0.5B INT8 (local)",
            }
        ]

    def _local_detect(self, encoded: Any, object_name: str) -> list[dict[str, float]]:
        """Call the local model's ``detect`` and filter to well-formed boxes.

        Mirrors :meth:`MoondreamCloudAnalyzer._detect_objects` for behavioural
        parity between the two providers. Returns ``[]`` on any error so
        callers can safely skip detect-based logic when it fails.
        """
        try:
            objects = self._md_model.detect(encoded, object_name).get("objects", [])
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Moondream local detect failed for %r: %s", object_name, exc)
            return []
        return [o for o in objects if isinstance(o, dict)]

    def _local_caption(self, encoded: Any) -> str:
        """Call the local model's ``caption`` for factual grounding context.

        Mirrors :meth:`MoondreamCloudAnalyzer._caption_frame`, including the
        ``length="short"`` choice — see that method's docstring for why.
        Returns "" on any error so callers can skip the grounding hint.
        """
        try:
            return str(
                self._md_model.caption(encoded, length="short").get("caption", "")
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Moondream local caption failed: %s", exc)
            return ""

    def _detect_protected_vehicle_sync(
        self, encoded: Any, all_car_boxes: list[dict[str, float]]
    ) -> tuple[list[dict[str, float]], list[dict[str, float]]]:
        """Local-inference counterpart to
        :meth:`MoondreamCloudAnalyzer._detect_protected_vehicle` —
        disambiguates the protected vehicle from other cars in frame using
        its own description, but only when more than one car box is
        present (see that method for the full rationale). Returns
        ``(protected_vehicle_boxes, other_vehicle_boxes)``.
        """
        all_car_boxes = self._dedupe_boxes(all_car_boxes)
        if len(all_car_boxes) <= 1 or not self._car_description:
            return all_car_boxes, []

        protected_boxes = self._local_detect(
            encoded, self._visual_detect_query(self._car_description)
        )
        if not protected_boxes:
            return all_car_boxes, []

        other_boxes = self._other_vehicle_boxes(protected_boxes, all_car_boxes)
        return protected_boxes, other_boxes

    def _analyze_frame_sync(
        self, frame_bytes: bytes, prompt: str, car_applies: bool
    ) -> str:
        """Run the full detect + caption + query pipeline for one frame.

        Runs entirely inside the thread executor since every Moondream local
        call is blocking. The image is encoded once via ``encode_image`` and
        the encoded result is reused across every detect/caption/query call
        on this frame — encoding is the expensive vision-tower pass, so
        reusing it avoids repeating that work up to five times per frame.
        Mirrors :meth:`MoondreamCloudAnalyzer._call_model`'s per-frame
        pipeline; see that method's docstring for the phase-by-phase
        rationale.
        """
        import io

        from PIL import Image

        image = Image.open(io.BytesIO(frame_bytes))
        encoded = self._md_model.encode_image(image)

        persons, animals, protected_boxes, other_vehicle_boxes, generic_vehicles = (
            self._local_detect_frame_subjects(encoded, car_applies)
        )
        multiple_vehicles = car_applies and bool(other_vehicle_boxes)

        if (
            not persons
            and not animals
            and not multiple_vehicles
            and not generic_vehicles
        ):
            return self._no_subject_response()

        augmented_prompt, multiple_vehicles = self._local_augment_prompt_for_frame(
            encoded,
            prompt,
            car_applies,
            persons,
            animals,
            protected_boxes,
            other_vehicle_boxes,
            generic_vehicles,
            multiple_vehicles,
        )

        caption = self._local_caption(encoded)
        if caption:
            augmented_prompt += (
                "\n\n[INTERNAL SCENE CAPTION — factual grounding only, do NOT "
                f"copy this text verbatim into your description]: {caption}"
            )

        # No reasoning=True here (unlike MoondreamCloudAnalyzer._call_api_frame):
        # the local `moondream` package's query() signature is
        # query(image, question, stream=False) — it has no reasoning parameter.
        # Reasoning mode is a Moondream Cloud-only capability, not an oversight.
        result = self._md_model.query(encoded, augmented_prompt)
        answer = str(result.get("answer", ""))
        subjects = persons + animals
        if multiple_vehicles and not subjects:
            answer = self._force_not_suspicious(answer)
        return answer

    def _local_detect_frame_subjects(
        self, encoded: Any, car_applies: bool
    ) -> tuple[
        list[dict[str, float]],
        list[dict[str, float]],
        list[dict[str, float]],
        list[dict[str, float]],
        list[dict[str, float]],
    ]:
        """Sync counterpart of MoondreamCloudAnalyzer._detect_frame_subjects."""
        persons = self._local_detect(encoded, "person")

        animals: list[dict[str, float]] = []
        protected_boxes: list[dict[str, float]] = []
        other_vehicle_boxes: list[dict[str, float]] = []
        generic_vehicles: list[dict[str, float]] = []
        if not persons:
            animals = self._local_detect(encoded, "animal")
            if car_applies:
                all_car_boxes = self._local_detect(encoded, "car")
                protected_boxes, other_vehicle_boxes = (
                    self._detect_protected_vehicle_sync(encoded, all_car_boxes)
                )
            else:
                generic_vehicles = self._local_detect(encoded, "vehicle")

        return persons, animals, protected_boxes, other_vehicle_boxes, generic_vehicles

    def _local_augment_prompt_for_frame(
        self,
        encoded: Any,
        prompt: str,
        car_applies: bool,
        persons: list[dict[str, float]],
        animals: list[dict[str, float]],
        protected_boxes: list[dict[str, float]],
        other_vehicle_boxes: list[dict[str, float]],
        generic_vehicles: list[dict[str, float]],
        multiple_vehicles: bool,
    ) -> tuple[str, bool]:
        """Sync counterpart of MoondreamCloudAnalyzer._augment_prompt_for_frame."""
        subjects = persons + animals

        if car_applies:
            return self._local_augment_car_prompt(
                encoded,
                prompt,
                subjects,
                protected_boxes,
                other_vehicle_boxes,
                multiple_vehicles,
            )

        return (
            self._augment_noncar_prompt(prompt, persons, animals, generic_vehicles),
            multiple_vehicles,
        )

    def _local_augment_car_prompt(
        self,
        encoded: Any,
        prompt: str,
        subjects: list[dict[str, float]],
        protected_boxes: list[dict[str, float]],
        other_vehicle_boxes: list[dict[str, float]],
        multiple_vehicles: bool,
    ) -> tuple[str, bool]:
        augmented_prompt = prompt
        camera = getattr(self, "_current_camera", "")

        if not protected_boxes and not other_vehicle_boxes:
            all_car_boxes = self._local_detect(encoded, "car")
            protected_boxes, other_vehicle_boxes = self._detect_protected_vehicle_sync(
                encoded, all_car_boxes
            )
            multiple_vehicles = bool(other_vehicle_boxes)

        if subjects and (protected_boxes or other_vehicle_boxes):
            # See MoondreamCloudAnalyzer._call_model's matching comment:
            # gap uses every detected car box, not just the one
            # disambiguation labelled "protected", so a person touching
            # the vehicle is never missed just because the two
            # independent detect calls' boxes disagree at the exact
            # moment of contact.
            gap = self._bbox_min_gap(subjects, protected_boxes + other_vehicle_boxes)
            augmented_prompt += f"\n\n{self._proximity_hint(gap, 'person or animal')}"
        elif multiple_vehicles:
            gap = (
                self._bbox_min_gap(protected_boxes, other_vehicle_boxes)
                if protected_boxes
                else self._bbox_min_pairwise_gap(other_vehicle_boxes)
            )
            augmented_prompt += f"\n\n{self._vehicle_proximity_hint(gap)}"
        elif subjects and self._car_zones.get(camera):
            # See MoondreamCloudAnalyzer._call_model's matching comment:
            # fall back to the fixed car zone when detect found no car
            # box at all this frame, so a person standing where the car
            # normally is still gets flagged.
            zone = self._car_zone_bbox(self._car_zones[camera])
            gap = self._bbox_min_gap(subjects, [zone])
            augmented_prompt += f"\n\n{self._proximity_hint(gap, 'person or animal')}"
        # else: car detect returned nothing and no zone is configured — no
        # proximity hint; the base prompt's vehicle-distance rules still
        # apply if the model can see the car in the frames.

        return augmented_prompt, multiple_vehicles

    async def _call_model(self, frames: list[bytes], prompt: str) -> str:
        """Run the local model on all extracted frames and return the most alarming result."""
        if not await self._ensure_model():
            return ""
        if not frames:
            return ""

        camera = getattr(self, "_current_camera", "")
        car_applies = self._car_protection_applies(camera)

        best_response = ""
        best_is_suspicious = False
        best_confidence = 0.0

        for frame in frames:
            resp = await self._run_frame_inference(frame, prompt, car_applies)
            if not resp:
                continue

            susp, conf, desc = self._try_parse_json(resp)
            if not desc:
                if not best_response:
                    best_response = resp
                continue
            if self._is_better_frame_result(
                best_response, best_is_suspicious, best_confidence, susp, conf
            ):
                best_response = resp
                best_is_suspicious = susp
                best_confidence = conf

        return best_response

    async def _run_frame_inference(
        self, frame: bytes, prompt: str, car_applies: bool
    ) -> str:
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(
                None, self._analyze_frame_sync, frame, prompt, car_applies
            )
        except Exception:
            _LOGGER.exception("Moondream local inference failed")
            return ""

    @staticmethod
    def _is_better_frame_result(
        best_response: str,
        best_is_suspicious: bool,
        best_confidence: float,
        susp: bool,
        conf: float,
    ) -> bool:
        return (
            not best_response
            or (susp and not best_is_suspicious)
            or (susp == best_is_suspicious and conf > best_confidence)
        )
