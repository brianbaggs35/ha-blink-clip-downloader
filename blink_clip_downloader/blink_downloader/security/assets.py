"""Protected assets — the things on a property worth defending.

Historically this add-on only knew about one protected thing: "the car",
described by ``ai_car_description`` and optionally located by a per-camera
``car_zone``. :class:`ProtectedAsset` generalizes that without changing any
of it — a vehicle asset is still built from exactly those two settings, and
a camera with neither configured still has no asset.

The generalization buys three concrete things: the event detector and risk
scorer are written against "the asset" rather than "the car" (so they read
correctly and need no rewrite when doors or package areas gain
configuration); the real-world width table turns a pixel gap into an honest
distance per asset type instead of hardcoding a car's six feet everywhere;
and :class:`AssetLocation` makes explicit the difference between *we can
see it*, *we know where it usually is*, and *something else is parked there
and it appears to be gone* — a distinction the old "nearest vehicle wins"
logic had no way to express, and the direct cause of a neighbour's car
being treated as the protected one.
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
    PACKAGE_AREA = "package_area"
    MAILBOX = "mailbox"
    CAMERA = "camera"
    OTHER = "other"


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
    AssetType.PACKAGE_AREA: 3.0,
    AssetType.MAILBOX: 1.0,
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
