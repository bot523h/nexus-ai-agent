"""``nexus.color.delivery`` — Local color grading, LUT profiles, proxy generation,
and OTIO delivery (Wave 7 substrate).

This package provides the pure substrate for Nagar color grading and delivery:
* typed data models (:class:`ApplyLutInput`, :class:`AdjustExposureInput`,
  :class:`AutoBalanceInput`, :class:`MakeProxyInput`, :class:`ExportOtioInput`,
  :class:`RenderMaster4KInput`);
* OpenTimelineIO schema structures (:class:`RationalTime`, :class:`TimeRange`,
  :class:`OtioClip`, :class:`OtioTrack`);
* pure capability operations: ``color.apply_lut``, ``color.adjust_exposure``,
  ``color.auto_balance``, ``delivery.make_proxy_480p``, ``delivery.export_otio``,
  and ``delivery.render_master_4k``.

In accordance with Nagar substrate architecture, this package has zero heavy dependencies
(no PyTorch, opencv, or external C-extensions).
"""

from __future__ import annotations

from nexus_ai_agent.creative.packs.delivery.models import (
    DELIVERY_PACKAGE_ID,
    DOMAIN_COLOR,
    DOMAIN_DELIVERY,
    OPERATION_ADJUST_EXPOSURE,
    OPERATION_APPLY_LUT,
    OPERATION_AUTO_BALANCE,
    OPERATION_EXPORT_OTIO,
    OPERATION_MAKE_PROXY,
    OPERATION_MATCH_SHOT,
    OPERATION_RENDER_MASTER_4K,
    AdjustExposureInput,
    ApplyLutInput,
    AutoBalanceInput,
    ExportOtioInput,
    MakeProxyInput,
    MatchShotInput,
    OtioClip,
    OtioTrack,
    RationalTime,
    RenderMaster4KInput,
    TimeRange,
)
from nexus_ai_agent.creative.packs.delivery.operations import (
    build_delivery_registry,
    register_delivery_operations,
)

__all__ = [
    "DELIVERY_PACKAGE_ID",
    "DOMAIN_COLOR",
    "DOMAIN_DELIVERY",
    "OPERATION_ADJUST_EXPOSURE",
    "OPERATION_APPLY_LUT",
    "OPERATION_AUTO_BALANCE",
    "OPERATION_EXPORT_OTIO",
    "OPERATION_MAKE_PROXY",
    "OPERATION_MATCH_SHOT",
    "OPERATION_RENDER_MASTER_4K",
    "AdjustExposureInput",
    "ApplyLutInput",
    "AutoBalanceInput",
    "ExportOtioInput",
    "MakeProxyInput",
    "MatchShotInput",
    "OtioClip",
    "OtioTrack",
    "RationalTime",
    "RenderMaster4KInput",
    "TimeRange",
    "build_delivery_registry",
    "register_delivery_operations",
]
