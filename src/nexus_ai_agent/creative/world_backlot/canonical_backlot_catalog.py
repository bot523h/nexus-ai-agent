"""A lazy 12-archive procedural registry and a bilingual, offline scene resolver.

The default catalogue has 122,880 distinct parameter profiles, NOT 122,880 binary
files. The six character/performance archives provide typed procedural control
profiles; this isolated package does not pretend to render faces or synthesize
speech. The world archives materialize geometry, sky, particles, camera, LUT and
ambient WAV. Matching is explicit Persian/English lexicon matching, not an LLM.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, replace
from enum import Enum
from types import MappingProxyType
from typing import TypeAlias

from ._determinism import (
    LICENSE,
    MAX_AUDIO_SECONDS,
    MAX_POINTS,
    SAMPLE_RATE,
    SCHEMA,
    FloatArray,
    Seed,
    digest,
    finite,
    integer,
    seed_bytes,
    uniform,
)
from .architecture_decor_grammar import (
    ArchitectureSpec,
    ArchitectureStyle,
    CameraCurve,
    CameraKeyframe,
    SceneLayout,
    generate_architecture,
    generate_camera_curve,
)
from .celestial_spacetime_sky import (
    Atmosphere,
    CubeLUT,
    Observer,
    SkySpectrum,
    SkyState,
    StarField,
    celestial_state,
    generate_cube,
    generate_starfield,
    sky_spectrum,
)
from .soil_air_weather_physics import (
    AmbientAudio,
    CurlWind,
    ParticleBounds,
    ParticleState,
    SurfaceKind,
    SurfaceResponse,
    WeatherKind,
    generate_curl_wind,
    initialize_particles,
    particle_physics,
    soil_optics,
    step_particles,
    synthesize_ambient,
)


class ArchiveKind(str, Enum):
    FACE = "face"
    BODY = "body"
    CLOTHING = "clothing"
    MOTION = "motion"
    VOICE = "voice"
    SPOKEN_LANGUAGE = "spoken_language"
    ARCHITECTURE = "architecture"
    DECOR = "decor"
    SPACETIME = "spacetime"
    WEATHER = "weather"
    LIGHTING = "lighting"
    CAMERA = "camera"


@dataclass(frozen=True, slots=True)
class ArchiveFamily:
    key: str
    tags_en: tuple[str, ...]
    tags_fa: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ArchiveSpec:
    kind: ArchiveKind
    label_en: str
    label_fa: str
    families: tuple[ArchiveFamily, ...]


def _families(rows: tuple[tuple[str, str, str], ...]) -> tuple[ArchiveFamily, ...]:
    return tuple(
        ArchiveFamily(key, (key.replace("_", " "), *en.split("|")), tuple(fa.split("|")))
        for key, en, fa in rows
    )


def _archive_specs() -> tuple[ArchiveSpec, ...]:
    rows: tuple[tuple[ArchiveKind, str, str, tuple[tuple[str, str, str], ...]], ...] = (
        (
            ArchiveKind.FACE,
            "face",
            "چهره",
            (
                ("oval", "adult|balanced", "بیضی|بزرگسال"),
                ("round", "soft", "گرد|نرم"),
                ("square", "strong jaw", "مربع|فک"),
                ("heart", "tapered", "قلبی"),
                ("long", "elongated", "کشیده"),
                ("diamond", "cheekbones", "لوزی|گونه"),
                ("mature", "adult|aged", "بالغ|سالخورده"),
                ("young_adult", "adult", "جوان|بزرگسال"),
                ("freckled", "freckles", "کک مک"),
                ("angular", "sharp", "زاویه دار"),
            ),
        ),
        (
            ArchiveKind.BODY,
            "body",
            "اندام",
            (
                ("petite", "short", "کوتاه"),
                ("medium", "average", "متوسط"),
                ("tall", "height", "بلند"),
                ("athletic", "sport", "ورزیده"),
                ("broad", "shoulders", "چهارشانه"),
                ("slender", "slim", "باریک"),
                ("stocky", "compact", "تنومند"),
                ("long_limb", "limbs", "دست و پای بلند"),
                ("balanced", "proportions", "متناسب"),
                ("seated", "sitting", "نشسته"),
            ),
        ),
        (
            ArchiveKind.CLOTHING,
            "clothing",
            "لباس",
            (
                ("linen", "casual", "کتان"),
                ("silk", "formal", "ابریشم"),
                ("wool", "winter", "پشمی|زمستانی"),
                ("denim", "urban", "جین|شهری"),
                ("leather", "jacket", "چرم|کت"),
                ("robe", "traditional", "ردا|سنتی"),
                ("suit", "formal", "کت و شلوار"),
                ("workwear", "industrial", "لباس کار"),
                ("spacesuit", "space", "لباس فضایی"),
                ("raincoat", "rain", "بارانی"),
            ),
        ),
        (
            ArchiveKind.MOTION,
            "motion",
            "حرکت",
            (
                ("walk", "walking", "راه رفتن"),
                ("run", "running", "دویدن"),
                ("idle", "standing", "ایستادن"),
                ("sit", "sitting", "نشستن"),
                ("turn", "rotation", "چرخیدن"),
                ("reach", "arm", "دست دراز کردن"),
                ("gesture", "hands", "اشاره"),
                ("climb", "stairs", "بالا رفتن"),
                ("jump", "airborne", "پریدن"),
                ("dance", "rhythmic", "رقص"),
            ),
        ),
        (
            ArchiveKind.VOICE,
            "voice and tone",
            "صدا و لحن",
            (
                ("warm", "gentle", "گرم|ملایم"),
                ("bright", "clear", "روشن|شفاف"),
                ("deep", "bass", "بم"),
                ("soft", "quiet", "آرام"),
                ("breathy", "air", "نفس آلود"),
                ("raspy", "rough", "خش دار"),
                ("neutral", "balanced", "خنثی"),
                ("urgent", "fast", "فوری|سریع"),
                ("narrator", "story", "راوی"),
                ("whisper", "quiet", "نجوا"),
            ),
        ),
        (
            ArchiveKind.SPOKEN_LANGUAGE,
            "spoken language",
            "زبان گفتاری",
            (
                ("fa", "persian|farsi", "فارسی"),
                ("en", "english", "انگلیسی"),
                ("ar", "arabic", "عربی"),
                ("tr", "turkish", "ترکی"),
                ("fr", "french", "فرانسوی"),
                ("de", "german", "آلمانی"),
                ("es", "spanish", "اسپانیایی"),
                ("hi", "hindi", "هندی"),
                ("ja", "japanese", "ژاپنی"),
                ("ko", "korean", "کره ای"),
            ),
        ),
        (
            ArchiveKind.ARCHITECTURE,
            "architecture",
            "ساختمان معماری",
            (
                (
                    "iranian_caravanserai",
                    "iran|persian|traditional|desert|caravanserai",
                    "ایران|ایرانی|سنتی|کاروانسرا|کویر",
                ),
                (
                    "persian_courtyard",
                    "iran|iwan|badgir|orsi|girih",
                    "ایوان|بادگیر|ارسی|گره چینی|حیاط",
                ),
                ("eastern_temple", "eastern|dome", "شرقی|معبد|گنبد"),
                ("glass_tower", "modern|urban|skyscraper", "مدرن|شهری|برج شیشه ای"),
                ("urban_apartment", "modern|street|bridge", "آپارتمان|خیابان|پل"),
                ("medieval_castle", "historic|castle", "تاریخی|قلعه"),
                ("classical_palace", "classical|palace|hall", "کلاسیک|کاخ|تالار"),
                (
                    "cyberpunk_factory",
                    "industrial|cyberpunk|warehouse|tunnel|neon",
                    "صنعتی|سایبرپانک|کارخانه|انبار|تونل|نئون",
                ),
                (
                    "orbital_station",
                    "space|scifi|spaceship|corridor",
                    "فضایی|علمی تخیلی|ایستگاه مداری|سفینه|راهرو",
                ),
                ("planetary_base", "space|planet|base", "پایگاه سیاره ای"),
            ),
        ),
        (
            ArchiveKind.DECOR,
            "decor and props",
            "دکور قطعات",
            (
                ("table", "furniture", "میز"),
                ("chair", "furniture", "صندلی"),
                ("lamp", "light", "چراغ"),
                ("column", "pillar", "ستون"),
                ("tree", "foliage", "درخت"),
                ("rock", "stone", "صخره|سنگ"),
                ("stage_prop", "prop", "وسایل صحنه"),
                ("neon_sign", "neon", "تابلو نئون"),
                ("rug", "carpet", "فرش"),
                ("planter", "plant", "گلدان"),
            ),
        ),
        (
            ArchiveKind.SPACETIME,
            "spacetime sky",
            "فضا زمان آسمان",
            (
                ("dawn", "sunrise", "سپیده دم|طلوع"),
                ("morning", "early", "صبح"),
                ("noon", "midday", "ظهر"),
                ("afternoon", "day", "بعدازظهر"),
                ("sunset", "golden hour", "غروب|ساعت طلایی"),
                ("twilight", "dusk", "شفق|گرگ و میش"),
                ("midnight", "night|starry|stars", "نیمه شب|شب|پرستاره|ستاره|ستارگان"),
                ("full_moon", "lunar phase", "ماه کامل|بدر"),
                ("new_moon", "lunar phase|eclipse", "ماه نو|کسوف|خسوف"),
                ("milky_way", "galaxy|starfield", "راه شیری|کهکشان"),
            ),
        ),
        (
            ArchiveKind.WEATHER,
            "soil air weather",
            "خاک هوا آب و هوا",
            (
                ("clear", "dry|soil|sand", "صاف|خشک|خاک|شن"),
                ("wind", "breeze", "باد|نسیم"),
                ("rain", "rainfall|wet", "باران|بارانی|مرطوب"),
                ("storm", "thunder|lightning", "طوفان|رعد|برق"),
                ("snow", "snowfall", "برف|برفی"),
                ("dust", "desert|sandstorm", "گردوغبار|گرد و غبار|غبار|کویری"),
                ("ash", "volcanic", "خاکستر|آتشفشان"),
                ("leaves", "autumn", "برگ|پاییزی"),
                ("fog", "mist", "مه|مه آلود"),
                ("sparks", "fire|embers", "جرقه|آتش"),
            ),
        ),
        (
            ArchiveKind.LIGHTING,
            "lighting color cube",
            "نور رنگ کیوب",
            (
                ("daylight", "sun|noon|cube", "روز|آفتاب|ظهر"),
                ("golden", "sunset|warm", "طلایی|غروب|گرم"),
                ("moonlight", "blue|moon|night", "مهتاب|آبی|ماه|شب"),
                ("overcast", "diffuse", "ابری|پخش"),
                ("neon", "cyberpunk", "نئون|سایبرپانک"),
                ("candle", "fire|warm", "شمع|آتش"),
                ("studio", "softbox", "استودیو|سافت باکس"),
                ("silhouette", "backlight", "ضدنور|سیلوئت"),
                ("high_key", "bright", "روشن"),
                ("low_key", "dark", "تاریک"),
            ),
        ),
        (
            ArchiveKind.CAMERA,
            "director camera",
            "کارگردانی دوربین",
            (
                ("static", "locked", "ثابت"),
                ("dolly", "push", "دالی"),
                ("orbit", "around", "مدار|چرخش دوربین"),
                ("crane", "rise", "کرین|بالارونده"),
                ("handheld", "hand held", "دوربین روی دست"),
                ("tracking", "track", "تراولینگ|تعقیبی"),
                ("wide", "wide angle", "واید|باز"),
                ("closeup", "close up", "کلوزآپ|نمای نزدیک"),
                ("establishing", "establish", "معرف"),
                ("low_angle", "low angle", "زاویه پایین"),
            ),
        ),
    )
    return tuple(ArchiveSpec(kind, en, fa, _families(families)) for kind, en, fa, families in rows)


_ARCHIVES = _archive_specs()

_STYLES = {
    "iranian_caravanserai": ArchitectureStyle.IRANIAN_EASTERN,
    "persian_courtyard": ArchitectureStyle.IRANIAN_EASTERN,
    "eastern_temple": ArchitectureStyle.IRANIAN_EASTERN,
    "glass_tower": ArchitectureStyle.MODERN_URBAN,
    "urban_apartment": ArchitectureStyle.MODERN_URBAN,
    "medieval_castle": ArchitectureStyle.CLASSICAL_HISTORIC,
    "classical_palace": ArchitectureStyle.CLASSICAL_HISTORIC,
    "cyberpunk_factory": ArchitectureStyle.INDUSTRIAL_CYBERPUNK,
    "orbital_station": ArchitectureStyle.SPACE_SCIFI,
    "planetary_base": ArchitectureStyle.SPACE_SCIFI,
}
_HOURS = {
    "dawn": 5.0,
    "morning": 8.0,
    "noon": 12.0,
    "afternoon": 15.0,
    "sunset": 18.0,
    "twilight": 19.5,
    "midnight": 0.0,
    "full_moon": 0.0,
    "new_moon": 0.0,
    "milky_way": 0.0,
}
Parameter: TypeAlias = tuple[str, float | str]


def normalize_query(text: str) -> str:
    if not isinstance(text, str) or len(text) > 4096:
        raise ValueError("query must be a string of at most 4096 characters")
    text = (
        unicodedata.normalize("NFKC", text)
        .casefold()
        .translate(str.maketrans("يىك\u200c_", "ییک  ", "ـ"))
    )
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")
    return " ".join(re.findall(r"\w+", text))


def _profile(kind: ArchiveKind, family: str, seed: Seed) -> tuple[Parameter, ...]:
    r = [float(x) for x in uniform(seed, "catalog/profile", 6)]
    if kind is ArchiveKind.FACE:
        return (
            ("width_height", 0.65 + 0.2 * r[0]),
            ("jaw_width", 0.6 + 0.35 * r[1]),
            ("eye_spacing", 0.28 + 0.08 * r[2]),
            ("nose_length", 0.24 + 0.12 * r[3]),
        )
    if kind is ArchiveKind.BODY:
        return (
            ("height_m", 1.45 + 0.65 * r[0]),
            ("shoulder_height_ratio", 0.2 + 0.12 * r[1]),
            ("leg_height_ratio", 0.43 + 0.10 * r[2]),
            ("arm_height_ratio", 0.38 + 0.09 * r[3]),
        )
    if kind is ArchiveKind.CLOTHING:
        return (
            ("roughness", 0.15 + 0.8 * r[0]),
            ("weave_mm", 0.1 + 2 * r[1]),
            ("drape_stiffness", 0.1 + 0.8 * r[2]),
            ("hue_degrees", 360 * r[3]),
        )
    if kind is ArchiveKind.MOTION:
        return (
            ("cycle_seconds", 0.4 + 2 * r[0]),
            ("stride_m", 0.2 + 1.4 * r[1]),
            ("joint_swing_rad", 0.1 + 1.2 * r[2]),
            ("phase_rad", 2 * math.pi * r[3]),
        )
    if kind is ArchiveKind.VOICE:
        return (
            ("fundamental_hz", 80 + 240 * r[0]),
            ("formant_scale", 0.8 + 0.4 * r[1]),
            ("breathiness", 0.02 + 0.3 * r[2]),
            ("intonation_semitones", 1 + 8 * r[3]),
        )
    if kind is ArchiveKind.SPOKEN_LANGUAGE:
        return (
            ("bcp47", family),
            ("direction", "rtl" if family in ("fa", "ar") else "ltr"),
            ("syllables_per_second", 3 + 4 * r[0]),
            ("pause_seconds", 0.1 + 0.5 * r[1]),
        )
    if kind is ArchiveKind.ARCHITECTURE:
        return (
            ("style", _STYLES[family].value),
            ("width_m", 18 + 18 * r[0]),
            ("height_m", 6 + 12 * r[1]),
            ("depth_m", 12 + 20 * r[2]),
            ("floors", float(1 + int(4 * r[3]))),
            ("bays", float(3 + int(5 * r[4]))),
        )
    if kind is ArchiveKind.DECOR:
        return (
            ("scale_m", 0.2 + 3 * r[0]),
            ("roughness", 0.1 + 0.85 * r[1]),
            ("hue_degrees", 360 * r[2]),
            ("depth_m", 2 + 75 * r[3]),
        )
    if kind is ArchiveKind.SPACETIME:
        return (
            ("hour", _HOURS[family]),
            ("latitude_deg", -60 + 120 * r[0]),
            ("day_of_year", float(1 + int(365 * r[1]))),
            ("turbidity", 1 + 4 * r[2]),
        )
    if kind is ArchiveKind.WEATHER:
        physics = particle_physics(family)
        return (
            ("mass_kg", physics.mass_kg),
            ("drag_kg_s", physics.drag_kg_s),
            ("noise_n_sqrt_s", physics.noise_n_sqrt_s),
            ("wind_strength", r[0]),
            ("density", 0.1 + 0.9 * r[1]),
        )
    if kind is ArchiveKind.LIGHTING:
        return (
            ("exposure_ev", -0.75 + 1.5 * r[0]),
            ("haze", 0.01 + 0.1 * r[1]),
            ("saturation", 0.8 + 0.4 * r[2]),
            ("cube_size", 9.0),
        )
    return (
        ("template", family),
        ("duration_seconds", 4 + 8 * r[0]),
        ("focal_length_px", 600 + 900 * r[1]),
        ("travel_m", 0.2 + r[2]),
    )


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    entry_id: str
    archive: ArchiveKind
    family: str
    variant: int
    tags_en: tuple[str, ...]
    tags_fa: tuple[str, ...]
    seed_sha256: str
    parameters: tuple[Parameter, ...]
    license: str = LICENSE

    @property
    def sha256(self) -> str:
        return digest(asdict(self))


class CanonicalWorldBacklotCatalog:
    """120 indexed families x 1024 lazy variants, with bounded search pagination."""

    __slots__ = (
        "_seed",
        "_variants_per_family",
        "_archives",
        "_families",
        "_family_lookup",
        "_index",
    )

    def __init__(
        self, seed: Seed = "NAGAR canonical studio", variants_per_family: int = 1024
    ) -> None:
        integer(variants_per_family, "variants per family", 1, 4096)
        self._seed = seed_bytes(seed, "catalog")
        self._variants_per_family = variants_per_family
        self._archives = _ARCHIVES
        self._families = tuple((a, f) for a in self.archives for f in a.families)
        self._family_lookup = {(a.kind, f.key): (a, f) for a, f in self._families}
        index: dict[str, set[int]] = defaultdict(set)
        for i, (archive, family) in enumerate(self._families):
            text = " ".join((archive.label_en, archive.label_fa, *family.tags_en, *family.tags_fa))
            for token in normalize_query(text).split():
                index[token].add(i)
        self._index: Mapping[str, frozenset[int]] = MappingProxyType(
            {token: frozenset(indices) for token, indices in index.items()}
        )

    @property
    def variants_per_family(self) -> int:
        return self._variants_per_family

    @property
    def archives(self) -> tuple[ArchiveSpec, ...]:
        return self._archives

    def __len__(self) -> int:
        return len(self._families) * self.variants_per_family

    @property
    def combination_count(self) -> int:
        """One independently selectable profile from each of the twelve archives."""
        return math.prod(len(a.families) * self.variants_per_family for a in self.archives)

    def entry(self, archive: ArchiveKind | str, family: str, variant: int = 0) -> CatalogEntry:
        kind = ArchiveKind(archive)
        integer(variant, "variant", 0, self.variants_per_family - 1)
        try:
            spec, group = self._family_lookup[kind, family]
        except KeyError as exc:
            raise ValueError(f"unknown family {family!r} in {kind.value}") from exc
        entry_seed = seed_bytes(self._seed, f"entry/{kind.value}/{family}/{variant}").hex()
        entry_id = f"wb1.{kind.value}.{family}.{variant:04d}.{self._seed.hex()[:16]}"
        return CatalogEntry(
            entry_id,
            kind,
            family,
            variant,
            (spec.label_en, *group.tags_en),
            (spec.label_fa, *group.tags_fa),
            entry_seed,
            _profile(kind, family, bytes.fromhex(entry_seed)),
        )

    def get(self, entry_id: str) -> CatalogEntry:
        parts = entry_id.split(".")
        if len(parts) != 5 or parts[0] != "wb1" or parts[4] != self._seed.hex()[:16]:
            raise ValueError("invalid or foreign catalogue ID")
        if not re.fullmatch(r"[0-9]{4}", parts[3]):
            raise ValueError("noncanonical catalogue variant")
        return self.entry(parts[1], parts[2], int(parts[3]))

    def entry_at(self, index: int) -> CatalogEntry:
        integer(index, "catalogue index", 0, len(self) - 1)
        family_index, variant = divmod(index, self.variants_per_family)
        archive, family = self._families[family_index]
        return self.entry(archive.kind, family.key, variant)

    def iter_entries(self, archive: ArchiveKind | str | None = None) -> Iterator[CatalogEntry]:
        kind = None if archive is None else ArchiveKind(archive)
        for spec, family in self._families:
            if kind is None or kind is spec.kind:
                for variant in range(self.variants_per_family):
                    yield self.entry(spec.kind, family.key, variant)

    def search(
        self,
        query: str = "",
        archive: ArchiveKind | str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[CatalogEntry, ...]:
        """AND token search in bilingual tags. Only the requested page is materialized."""
        integer(limit, "search limit", 0, 100)
        integer(offset, "search offset", 0, len(self))
        kind = None if archive is None else ArchiveKind(archive)
        tokens = normalize_query(query).split()
        candidates = set(range(len(self._families)))
        for token in tokens:
            candidates.intersection_update(self._index.get(token, frozenset()))
        result: list[CatalogEntry] = []
        for index in sorted(candidates):
            spec, family = self._families[index]
            if kind is not None and kind is not spec.kind:
                continue
            if offset >= self.variants_per_family:
                offset -= self.variants_per_family
                continue
            for variant in range(offset, self.variants_per_family):
                if len(result) == limit:
                    return tuple(result)
                result.append(self.entry(spec.kind, family.key, variant))
            offset = 0
        return tuple(result)

    def resolve_director_scene_recipe(
        self,
        description: str,
        *,
        seed: Seed = 0,
        quality: RecipeQuality | None = None,
        observer: Observer | None = None,
    ) -> DirectorSceneRecipe:
        return _resolve(self, description, seed, quality or RecipeQuality(), observer)


@dataclass(frozen=True, slots=True)
class RecipeQuality:
    """Interactive defaults; higher quality is explicit and hard bounded.

    2048 stars, an initial 1024-particle field, 9^3 LUT, 0.25 s PCM and
    an 8 s camera curve are all eagerly generated; meshes/film frames are lazy.
    The 50 ms budget is a benchmark target after imports, not a universal SLA.
    """

    star_count: int = 2048
    particle_count: int = 1024
    cube_size: int = 9
    audio_seconds: float = 0.25
    camera_seconds: float = 8.0

    def __post_init__(self) -> None:
        integer(self.star_count, "star count", 0, MAX_POINTS)
        integer(self.particle_count, "particle count", 0, MAX_POINTS)
        integer(self.cube_size, "cube size", 2, 33)
        finite(self.audio_seconds, "audio duration", 1 / SAMPLE_RATE, MAX_AUDIO_SECONDS)
        finite(self.camera_seconds, "camera duration", 0.01, 3600)


def _has(text: str, *phrases: str) -> bool:
    """Whole phrase matching with local no/without/not/بدون/فاقد negation."""
    for phrase in phrases:
        normalized = normalize_query(phrase)
        for match in re.finditer(r"(?<!\w)" + re.escape(normalized) + r"(?!\w)", text):
            prefix = text[: match.start()].split()[-2:]
            negators = {"no", "without", "not", "بدون", "فاقد"}
            modifiers = {
                "heavy",
                "light",
                "strong",
                "dense",
                "thick",
                "any",
                "شدید",
                "ملایم",
                "غلیظ",
                "هیچ",
            }
            negated = bool(prefix and prefix[-1] in negators) or (
                len(prefix) == 2 and prefix[0] in negators and prefix[1] in modifiers
            )
            if not negated:
                return True
    return False


_TIME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("midnight", ("midnight", "نیمه شب")),
    ("dawn", ("dawn", "sunrise", "سپیده دم", "طلوع")),
    ("sunset", ("sunset", "golden hour", "غروب", "ساعت طلایی")),
    ("twilight", ("twilight", "dusk", "شفق", "گرگ و میش")),
    ("morning", ("morning", "صبح")),
    ("afternoon", ("afternoon", "بعدازظهر")),
    ("noon", ("noon", "midday", "ظهر")),
    ("full_moon", ("full moon", "ماه کامل", "بدر")),
    ("new_moon", ("new moon", "ماه نو", "کسوف", "خسوف", "eclipse")),
    ("milky_way", ("milky way", "راه شیری", "کهکشان")),
    ("midnight", ("night", "starry", "stars", "شب", "پرستاره", "ستارگان")),
)
_ARCH_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("planetary_base", ("planetary base", "پایگاه سیاره ای")),
    (
        "orbital_station",
        (
            "space",
            "scifi",
            "sci fi",
            "spaceship",
            "orbital station",
            "فضایی",
            "سفینه",
            "ایستگاه مداری",
        ),
    ),
    (
        "cyberpunk_factory",
        (
            "industrial",
            "cyberpunk",
            "factory",
            "warehouse",
            "صنعتی",
            "سایبرپانک",
            "کارخانه",
            "انبار",
        ),
    ),
    ("medieval_castle", ("castle", "medieval", "قلعه")),
    ("classical_palace", ("classical", "palace", "کلاسیک", "کاخ", "تالار")),
    ("glass_tower", ("glass tower", "skyscraper", "برج")),
    ("urban_apartment", ("modern", "urban", "apartment", "مدرن", "شهری", "آپارتمان")),
    ("persian_courtyard", ("courtyard", "iwan", "badgir", "حیاط", "ایوان", "بادگیر", "ارسی")),
    ("eastern_temple", ("eastern temple", "معبد")),
    (
        "iranian_caravanserai",
        ("iran", "iranian", "persian", "caravanserai", "ایران", "ایرانی", "سنتی", "کاروانسرا"),
    ),
)
_WEATHER_RULES: tuple[tuple[WeatherKind, tuple[str, ...]], ...] = (
    (WeatherKind.STORM, ("storm", "thunder", "thunderstorm", "طوفان", "رعد")),
    (WeatherKind.RAIN, ("rain", "rainy", "rainfall", "باران", "بارانی")),
    (WeatherKind.SNOW, ("snow", "snowfall", "برف", "برفی")),
    (WeatherKind.DUST, ("dust", "dusty", "sandstorm", "گردوغبار", "گرد و غبار", "غبار")),
    (WeatherKind.ASH, ("ash", "volcanic", "خاکستر", "آتشفشان")),
    (WeatherKind.LEAVES, ("leaves", "autumn", "برگ", "پاییزی")),
    (WeatherKind.FOG, ("fog", "mist", "مه", "مه آلود")),
    (WeatherKind.SPARKS, ("sparks", "embers", "جرقه", "آتش")),
    (WeatherKind.WIND, ("wind", "breeze", "باد", "نسیم")),
)


@dataclass(frozen=True, slots=True)
class SceneIntent:
    description: str
    time_family: str
    architecture_family: str
    weather_kinds: tuple[WeatherKind, ...]
    surface_kind: SurfaceKind
    wind_strength: float
    rain_intensity: float
    lighting_family: str
    camera_family: str
    starry: bool
    interior: bool
    warnings: tuple[str, ...]


def parse_scene_description(description: str) -> SceneIntent:
    text = normalize_query(description)
    if not text:
        raise ValueError("a nonempty scene description is required")
    time_matches = [key for key, phrases in _TIME_RULES if _has(text, *phrases)]
    arch_matches = [key for key, phrases in _ARCH_RULES if _has(text, *phrases)]
    weather = tuple(kind for kind, phrases in _WEATHER_RULES if _has(text, *phrases))
    if WeatherKind.STORM in weather:
        weather = tuple(k for k in weather if k is not WeatherKind.RAIN)
    mild = _has(text, "gentle", "mild", "light", "ملایم", "آرام")
    intense = _has(text, "heavy", "strong", "شدید", "تند")
    strength = 0.25 if mild else (0.9 if intense else 0.5)
    if not weather:
        strength = 0.1
    desert = _has(text, "desert", "sand", "کویر", "کویری", "شن")
    wet = WeatherKind.RAIN in weather or WeatherKind.STORM in weather
    surface = SurfaceKind.SAND if desert else SurfaceKind.DRY_SOIL
    if WeatherKind.SNOW in weather:
        surface = SurfaceKind.SNOW
    elif wet:
        surface = (
            SurfaceKind.WET_ASPHALT
            if _has(
                text,
                "urban",
                "modern",
                "city",
                "street",
                "شهری",
                "مدرن",
                "شهر",
                "خیابان",
                "asphalt",
                "آسفالت",
            )
            else SurfaceKind.WET_SOIL
        )
    elif _has(text, "rocky", "rock", "سنگی", "صخره"):
        surface = SurfaceKind.ROCK
    time_family = time_matches[0] if time_matches else "noon"
    light = "daylight"
    if time_family in ("midnight", "milky_way", "full_moon", "new_moon") or _has(
        text, "moonlight", "مهتاب"
    ):
        light = "moonlight"
    elif time_family in ("sunset", "dawn", "twilight"):
        light = "golden"
    elif _has(text, "neon", "نئون"):
        light = "neon"
    elif _has(text, "overcast", "ابری"):
        light = "overcast"
    camera = "dolly"
    for family in _ARCHIVES[-1].families:
        if _has(text, *family.tags_en, *family.tags_fa):
            camera = family.key
            break
    warnings: list[str] = []
    if not time_matches:
        warnings.append("No explicit time recognized; civil noon selected.")
    if not arch_matches:
        warnings.append("No architecture recognized; Iranian caravanserai selected.")
    return SceneIntent(
        description,
        time_family,
        arch_matches[0] if arch_matches else "iranian_caravanserai",
        weather or (WeatherKind.CLEAR,),
        surface,
        strength,
        strength,
        light,
        camera,
        _has(text, "starry", "stars", "milky way", "پرستاره", "ستاره", "ستارگان", "راه شیری"),
        _has(text, "interior", "indoors", "داخلی"),
        tuple(warnings),
    )


_MODELS = (
    ("ephemeris", "NOAA-Meeus Sun; perturbed elliptic Moon; topocentric parallax"),
    ("rayleigh", "beta_R = 1.32e-5 * (550/lambda_nm)^4 * pressure/101325"),
    ("mie", "P_HG = (1-g^2)/(4*pi*(1+g^2-2*g*cos_gamma)^(3/2))"),
    ("stars", "Planck B_lambda = 2*h*c^2/(lambda^5*expm1(h*c/(lambda*k*T)))"),
    ("wind", "w(x,t) = mean + curl(sum(A_j*sin(k_j.x + omega_j*t + phase_j)))"),
    ("particles", "m*dv = -gamma*(v-w)*dt + m*g_eff*dt + sigma*dW; dx = v*dt"),
    ("parallax", "u_prime = u + focal_px*delta_X/Z"),
    ("audio", "48000 Hz mono PCM16; filtered aeroacoustics, impacts, distance/343 thunder delay"),
)


@dataclass(frozen=True, slots=True)
class RecipeSeal:
    sha256: str


@dataclass(frozen=True, slots=True)
class SceneFrame:
    time_seconds: float
    camera: CameraKeyframe
    star_directions: FloatArray
    star_flux: FloatArray
    particles: tuple[ParticleState, ...]
    scene_dna_sha256: str


@dataclass(frozen=True, slots=True)
class DirectorSceneRecipe:
    intent: SceneIntent
    selected_assets: tuple[CatalogEntry, ...]
    seed_sha256: str
    layout: SceneLayout
    sky_state: SkyState
    spectrum: SkySpectrum
    atmosphere: Atmosphere
    starfield: StarField
    wind: CurlWind
    particle_fields: tuple[ParticleState, ...]
    surface: SurfaceResponse
    cube: CubeLUT
    cube_text: str
    ambient_audio: AmbientAudio
    camera_curve: CameraCurve
    seal: RecipeSeal

    @property
    def sha256(self) -> str:
        return self.seal.sha256

    @property
    def equations(self) -> tuple[tuple[str, str], ...]:
        return _MODELS

    @property
    def wav_bytes(self) -> bytes:
        return self.ambient_audio.to_wav()

    def manifest(self) -> dict[str, object]:
        """Small serializable manifest, never copies particle/star/audio arrays."""
        return {
            "schema": SCHEMA,
            "license": LICENSE,
            "seed_sha256": self.seed_sha256,
            "intent": asdict(self.intent),
            "models": self.equations,
            "assets": tuple((a.entry_id, a.sha256) for a in self.selected_assets),
            "scene_dna": self.layout.seal.sha256,
            "sky": asdict(self.sky_state),
            "atmosphere": asdict(self.atmosphere),
            "spectrum": self.spectrum.sha256,
            "stars": self.starfield.sha256,
            "wind": self.wind.sha256,
            "particles": tuple(p.sha256 for p in self.particle_fields),
            "surface": asdict(self.surface),
            "cube": self.cube.sha256,
            "cube_file": hashlib.sha256(self.cube_text.encode("utf-8")).hexdigest(),
            "wav_file": self.ambient_audio.sha256,
            "camera": self.camera_curve.sha256,
        }

    def verify(self) -> bool:
        return (
            self.layout.verify()
            and self.cube_text == self.cube.to_cube()
            and self.seal.sha256 == digest(self.manifest())
        )

    def iter_frames(self, frame_count: int, fps: float = 24.0) -> Iterator[SceneFrame]:
        integer(frame_count, "frame count", 0, 100_000)
        finite(fps, "fps", 1, 120)
        if (frame_count - 1) / fps > self.camera_curve.keyframes[-1].time_seconds:
            raise ValueError("frame sequence exceeds the sealed camera duration")
        if not self.verify():
            raise ValueError("scene recipe failed its integrity seal")
        states = self.particle_fields
        for index in range(frame_count):
            time = index / fps
            directions, flux = self.starfield.sample(self.sky_state, time)
            yield SceneFrame(
                time,
                self.camera_curve.sample(time),
                directions,
                flux,
                states,
                self.layout.seal.sha256,
            )
            if index + 1 < frame_count:
                states = tuple(
                    step_particles(p, 1 / fps, self.wind, bounds=ParticleBounds()) for p in states
                )


def _resolve(
    catalog: CanonicalWorldBacklotCatalog,
    description: str,
    seed: Seed,
    quality: RecipeQuality,
    observer: Observer | None,
) -> DirectorSceneRecipe:
    intent = parse_scene_description(description)
    root = seed_bytes(seed, "director")
    chosen = {
        ArchiveKind.FACE: "oval",
        ArchiveKind.BODY: "balanced",
        ArchiveKind.CLOTHING: "linen",
        ArchiveKind.MOTION: "idle",
        ArchiveKind.VOICE: "neutral",
        ArchiveKind.SPOKEN_LANGUAGE: "fa",
        ArchiveKind.ARCHITECTURE: intent.architecture_family,
        ArchiveKind.DECOR: "stage_prop",
        ArchiveKind.SPACETIME: intent.time_family,
        ArchiveKind.WEATHER: intent.weather_kinds[0].value,
        ArchiveKind.LIGHTING: intent.lighting_family,
        ArchiveKind.CAMERA: intent.camera_family,
    }
    assets = tuple(
        catalog.entry(
            kind,
            family,
            int.from_bytes(seed_bytes(root, kind.value)[:4], "big") % catalog.variants_per_family,
        )
        for kind, family in chosen.items()
    )
    architecture = dict(next(a for a in assets if a.archive is ArchiveKind.ARCHITECTURE).parameters)
    spec = ArchitectureSpec(
        _STYLES[intent.architecture_family],
        float(architecture["width_m"]),
        float(architecture["height_m"]),
        float(architecture["depth_m"]),
        int(float(architecture["floors"])),
        int(float(architecture["bays"])),
        intent.interior,
    )
    layout = generate_architecture(seed_bytes(root, "geometry"), spec)
    if observer is None:
        observer = Observer(32.6546, 172, _HOURS[intent.time_family], 2026, 51.668, 3.5)
    atmosphere = Atmosphere(turbidity=4.0 if WeatherKind.DUST in intent.weather_kinds else 2.0)
    sky = celestial_state(observer)
    spectrum = sky_spectrum(sky, atmosphere=atmosphere)
    stars = generate_starfield(seed_bytes(root, "stars"), quality.star_count)
    wind = generate_curl_wind(
        seed_bytes(root, "wind"),
        4 * intent.wind_strength,
        mean_velocity=(3 * intent.wind_strength, 0, 0),
    )
    count = quality.particle_count if intent.weather_kinds != (WeatherKind.CLEAR,) else 0
    quotient, remainder = divmod(count, len(intent.weather_kinds))
    fields = tuple(
        initialize_particles(
            seed_bytes(root, f"weather/{kind.value}"), kind, quotient + (i < remainder)
        )
        for i, kind in enumerate(intent.weather_kinds)
    )
    cube = generate_cube(spectrum, quality.cube_size)
    audio = synthesize_ambient(
        seed_bytes(root, "audio"),
        intent.weather_kinds[0],
        quality.audio_seconds,
        intent.wind_strength,
        intent.rain_intensity,
    )
    camera = generate_camera_curve(
        seed_bytes(root, "camera"), intent.camera_family, quality.camera_seconds
    )
    draft = DirectorSceneRecipe(
        intent,
        assets,
        root.hex(),
        layout,
        sky,
        spectrum,
        atmosphere,
        stars,
        wind,
        fields,
        soil_optics(intent.surface_kind),
        cube,
        cube.to_cube(),
        audio,
        camera,
        RecipeSeal(""),
    )
    return replace(draft, seal=RecipeSeal(digest(draft.manifest())))


def resolve_director_scene_recipe(
    description: str,
    *,
    seed: Seed = 0,
    quality: RecipeQuality | None = None,
    observer: Observer | None = None,
    catalog: CanonicalWorldBacklotCatalog | None = None,
) -> DirectorSceneRecipe:
    """Eagerly generate a bounded preview; no disk writes, caches, I/O or execution."""
    registry = catalog if catalog is not None else CanonicalWorldBacklotCatalog()
    return registry.resolve_director_scene_recipe(
        description, seed=seed, quality=quality, observer=observer
    )
