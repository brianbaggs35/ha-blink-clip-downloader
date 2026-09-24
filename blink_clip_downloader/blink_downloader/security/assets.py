"""Protected assets — the things on a property worth defending.

Historically this add-on only knew about one protected thing: "the car",
described by ``ai_car_description`` and optionally located by a per-camera
``car_zone``. :class:`ProtectedAsset` generalizes that without changing any
of it — a vehicle asset is still built from exactly those two settings, and
a camera with neither configured still has no asset.

The generalization buys three concrete things: the event detector and risk
scorer are written against "the asset" rather than "the car" (so they read
correctly for the doors, package spots and bikes the Assets tab marks); the
real-world width table turns a pixel gap into an honest distance per asset
type instead of hardcoding a car's six feet everywhere; and
:class:`AssetLocation` makes explicit the difference between *we can see
it*, *we know where it usually is*, and *something else is parked there and
it appears to be gone* — a distinction the old "nearest vehicle wins" logic
had no way to express, and the direct cause of a neighbour's car being
treated as the protected one.

Assets marked on the Assets tab (see :func:`build_marked_asset`) are located
by the zone the user drew rather than by detection: nothing in COCO is a
front door or a package spot, and a fixture that never moves does not need
finding. What *does* differ by type is what counts as ordinary use — a door
is walked up to and touched all day, a bike on its rack is not — and the
three properties at the bottom of :class:`ProtectedAsset` are where that is
decided, so no rule in the detector has to branch on a type name.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .geometry import Box, Zone, pixel_gap_to_feet
from .tracks import ObjectTrack
from .vehicles import (
    VehicleIdentification,
    VehicleSignature,
    identify_protected_vehicle,
)


class AssetType(StrEnum):
    """What kind of thing is being protected."""

    VEHICLE = "vehicle"
    DOOR = "door"
    WINDOW = "window"
    GARAGE = "garage"
    GATE = "gate"
    PACKAGE_AREA = "package_area"
    MAILBOX = "mailbox"
    BICYCLE = "bicycle"
    EQUIPMENT = "equipment"
    CAMERA = "camera"
    OTHER = "other"


#: The types a user can mark on the Assets tab. Vehicles have their own tab
#: (identification, signatures), and ``CAMERA`` is not something anyone
#: draws a zone around on its own frame.
MARKABLE_ASSET_TYPES: tuple[AssetType, ...] = (
    AssetType.DOOR,
    AssetType.WINDOW,
    AssetType.GARAGE,
    AssetType.GATE,
    AssetType.PACKAGE_AREA,
    AssetType.MAILBOX,
    AssetType.BICYCLE,
    AssetType.EQUIPMENT,
    AssetType.OTHER,
)

#: Assets people are *meant* to walk up to and touch: a visitor knocks on
#: the door, a courier puts a parcel on the spot, the post goes in the box.
#: Contact with one of these is its ordinary use, so the detector reports
#: presence, closeness and lingering at them but never claims contact —
#: summed, a delivery driver's confirmed touch of the front door scored in
#: the eighties, which would have forced an alert on every parcel. What is
#: concerning at a door (a handle tried, a lock forced) is judged by the AI
#: model from the frames, which the prompt directs to it.
_HANDLED_ROUTINELY: frozenset[AssetType] = frozenset(
    {
        AssetType.DOOR,
        AssetType.GATE,
        AssetType.GARAGE,
        AssetType.PACKAGE_AREA,
        AssetType.MAILBOX,
    }
)

#: Assets that are part of the building and cannot be moved, so the zone
#: drawn around one is exactly where it is rather than where it usually sits.
_FIXTURES: frozenset[AssetType] = frozenset(
    {
        AssetType.DOOR,
        AssetType.WINDOW,
        AssetType.GARAGE,
        AssetType.GATE,
        AssetType.MAILBOX,
    }
)


class AssetLocation(StrEnum):
    """How confidently the asset's position in frame is known."""

    #: The asset itself was detected in these frames.
    DETECTED = "detected"
    #: Not detected, but nothing contradicts its being there — fall back to
    #: the user-drawn zone, which is where it normally sits.
    ZONE = "zone"
    #: Other vehicles were detected and none of them is the protected one,
    #: so it appears to have been driven away. The zone still anchors area
    #: rules (someone standing in the empty space), but physical rules —
    #: proximity, contact, impact — are suppressed: there is nothing there
    #: to be close to or to touch.
    ZONE_ABSENT = "zone_absent"
    #: No zone, no detection: nothing to reason about geometrically.
    UNKNOWN = "unknown"


#: Approximate real-world width, in feet, of each asset type. Used only as
#: the scale reference that converts a pixel gap into a distance estimate —
#: a coarse figure is fine (and honest: the resulting estimate is reported
#: as approximate), an absent one is not, since without a reference a pixel
#: gap means nothing at all.
_ASSET_WIDTH_FEET: dict[AssetType, float] = {
    AssetType.VEHICLE: 6.0,
    AssetType.DOOR: 3.0,
    AssetType.WINDOW: 3.0,
    AssetType.GARAGE: 9.0,
    AssetType.GATE: 4.0,
    AssetType.PACKAGE_AREA: 3.0,
    AssetType.MAILBOX: 1.0,
    AssetType.BICYCLE: 5.5,
    AssetType.EQUIPMENT: 3.0,
    AssetType.CAMERA: 0.5,
    AssetType.OTHER: 3.0,
}


@dataclass
class ProtectedAsset:
    """One protected thing, as located in a specific camera's frames."""

    name: str
    asset_type: AssetType
    camera: str
    description: str = ""
    zone: Zone | None = None
    box: Box | None = None
    location: AssetLocation = AssetLocation.UNKNOWN
    identification: VehicleIdentification | None = None
    #: Stable id of a marked asset (see :func:`build_marked_asset`), which is
    #: how per-asset evidence — the depth/contact/pose stages' verdict, the
    #: before/after appearance change — is matched back to the right one.
    #: Empty for the protected vehicle, which is only ever one per clip.
    key: str = ""
    #: True when the user's own drawn zone *is* this asset rather than a
    #: stand-in for something detection is expected to find. A vehicle's
    #: zone marks where the car normally parks; a door's zone is the door.
    marked: bool = False

    @property
    def located(self) -> bool:
        """True when this asset has a usable position in frame."""
        return self.box is not None

    @property
    def detected(self) -> bool:
        """True when the asset itself was found, not just its usual spot."""
        return self.location is AssetLocation.DETECTED

    @property
    def present(self) -> bool:
        """True unless the asset appears to have been removed from the scene.

        Physical-interaction rules (proximity, contact, impact) require
        this: you cannot touch a car that has been driven away, and running
        those rules against the empty space it left is exactly how a
        neighbour parking in the vacated spot becomes a critical alert.
        """
        return self.location in (AssetLocation.DETECTED, AssetLocation.ZONE)

    @property
    def confident(self) -> bool:
        """True when the identification behind this asset is trustworthy."""
        return self.identification is None or self.identification.confident

    @property
    def width_feet(self) -> float:
        """Approximate real-world width of this asset type, in feet."""
        return _ASSET_WIDTH_FEET.get(
            self.asset_type, _ASSET_WIDTH_FEET[AssetType.OTHER]
        )

    @property
    def handled_routinely(self) -> bool:
        """True when touching this asset is its ordinary use (see
        :data:`_HANDLED_ROUTINELY`), so contact with it is not evidence."""
        return self.asset_type in _HANDLED_ROUTINELY

    @property
    def fixed(self) -> bool:
        """True when this asset cannot move, so where it was marked is where
        it is — a closeness measured against it is not a guess."""
        return self.asset_type in _FIXTURES

    @property
    def impact_applies(self) -> bool:
        """True when a strike on this asset is out of the ordinary.

        Only the vehicle. The impact rule fires on a confirmed touch plus a
        raised arm, a sudden speed-up, or the asset's own image changing —
        and knocking on a door is exactly a raised arm at a confirmed touch,
        while a door, gate or window opening changes its image completely.
        An impact is also the one event that withholds the face-recognition
        bypass (see ``BYPASS_BLOCKING_EVENTS``), so letting it fire on
        doors would make every household member who knocks permanently
        suspicious. A window being broken is what ``GLASS_BREAK_HEARD`` and
        the model's own reading of the frames exist for.
        """
        return self.asset_type is AssetType.VEHICLE

    @property
    def located_exactly(self) -> bool:
        """True when this asset's box is where it really is: detected in
        these frames, or a fixture the user marked."""
        return self.detected or (self.marked and self.fixed)

    @property
    def reference(self) -> str:
        """How event details name this asset: its description for the
        vehicle, the user's own name in quotes for a marked asset, so the
        prompt's evidence lines match its PROTECTED ASSETS list verbatim."""
        if self.marked and self.name:
            return f'"{self.name}"'
        return self.description or "the protected asset"

    def gap_feet(self, gap_pixels: float) -> float | None:
        """Convert a pixel gap to approximate feet using this asset as scale.

        ``None`` when the asset has no usable box to take a pixel width
        from — callers must then describe proximity qualitatively rather
        than inventing a distance.
        """
        if self.box is None:
            return None
        return pixel_gap_to_feet(gap_pixels, self.box[2] - self.box[0], self.width_feet)


def resolve_vehicle_asset(
    camera: str,
    description: str,
    tracks: list[ObjectTrack],
    frame_size: tuple[float, float],
    zone: Zone | None = None,
    signature: VehicleSignature | None = None,
    histograms: dict[int | None, tuple[float, ...]] | None = None,
) -> ProtectedAsset | None:
    """Locate the protected vehicle in this clip, or ``None`` if unprotected.

    Returns ``None`` when no *description* is configured — protected-vehicle
    rules have always required one (see
    ``BaseAnalyzer.car_protection_active``), and a camera with no protected
    vehicle must never produce vehicle events just because a car drove past.

    Which vehicle is "yours" is decided by
    :func:`~.vehicles.identify_protected_vehicle`, which weighs zone
    occupancy, this camera's learned parking position, and a learned colour
    fingerprint. When it finds no match among several detected vehicles, the
    asset is returned marked :attr:`AssetLocation.ZONE_ABSENT` rather than
    silently promoting the nearest car.
    """
    if not description:
        return None

    identification = identify_protected_vehicle(
        tracks, frame_size, zone=zone, signature=signature, histograms=histograms
    )
    zone_box = zone.to_pixel_box(*frame_size) if zone and frame_size[0] > 0 else None

    if identification.protected is not None:
        box: Box | None = identification.protected.box
        location = AssetLocation.DETECTED
    elif zone_box is not None:
        box = zone_box
        # "No vehicle detected at all" leaves open that the detector simply
        # missed a parked car in shadow, so the zone still stands in for it.
        # "Vehicles detected, none of them yours" does not.
        location = (
            AssetLocation.ZONE
            if not identification.others
            else AssetLocation.ZONE_ABSENT
        )
    else:
        box = None
        location = AssetLocation.UNKNOWN

    return ProtectedAsset(
        name=description,
        asset_type=AssetType.VEHICLE,
        camera=camera,
        description=description,
        zone=zone,
        box=box,
        location=location,
        identification=identification,
    )


def build_marked_asset(
    camera: str,
    key: str,
    name: str,
    asset_type: AssetType | str,
    zone: Zone | None,
    frame_size: tuple[float, float],
) -> ProtectedAsset | None:
    """Locate one asset marked on the Assets tab, or ``None`` if unusable.

    Located by its zone alone — see the module docstring for why detection
    plays no part — and always :attr:`AssetLocation.ZONE`: a front door does
    not get driven away, and a bike that has been taken is exactly what the
    before/after appearance change is there to notice, not a reason to stop
    watching its spot. ``None`` for a missing zone, an unmeasurable frame, or
    a type this build does not know, which every caller treats as "this
    asset contributes nothing to this clip" rather than as an error.
    """
    if zone is None or frame_size[0] <= 0 or frame_size[1] <= 0:
        return None
    try:
        kind = AssetType(asset_type)
    except ValueError:
        return None
    return ProtectedAsset(
        name=name,
        asset_type=kind,
        camera=camera,
        description=name,
        zone=zone,
        box=zone.to_pixel_box(*frame_size),
        location=AssetLocation.ZONE,
        key=key,
        marked=True,
    )
