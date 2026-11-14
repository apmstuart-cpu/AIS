"""Parse AISStream.io JSON messages into normalised position / static records.

AISStream message envelope (https://aisstream.io/documentation):
    {"MessageType": "PositionReport",
     "MetaData": {"MMSI": 235..., "ShipName": "...", "latitude": .., "longitude": ..,
                  "time_utc": "2024-05-01 12:34:56.123456789 +0000 UTC"},
     "Message": {"PositionReport": {"Sog": 6.2, "Cog": 181.3, "TrueHeading": 180,
                 "NavigationalStatus": 0, "Timestamp": 55, "Latitude": .., "Longitude": ..,
                 "Valid": true, "UserID": 235..., ...}}}

AISStream decodes the raw AIS fields: Sog is in knots (0.1 kn resolution),
Cog/TrueHeading in degrees. AIS "not available" sentinels survive decoding and are
mapped to None here:
    SOG 102.3 (raw 1023) not available; 102.2 means >=102.2 kn (treated as invalid)
    COG 360.0 (raw 3600) not available
    TrueHeading 511 not available
    Latitude 91 / Longitude 181 not available
    Timestamp (UTC second) 60 not available, 61 manual, 62 dead reckoning, 63 inoperative
MetaData.time_utc is when AISStream received the message, not the transponder's clock;
it is stored as provider_time_utc alongside our own ingestion time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .geo import parse_aisstream_time

POSITION_TYPES = {
    "PositionReport": "A",  # message types 1, 2, 3
    "StandardClassBPositionReport": "B",  # message type 18
    "ExtendedClassBPositionReport": "B",  # message type 19
}
STATIC_TYPES = ("ShipStaticData", "StaticDataReport")
SUBSCRIBE_TYPES = list(POSITION_TYPES) + list(STATIC_TYPES)

NAV_STATUS = {
    0: "under way using engine", 1: "at anchor", 2: "not under command",
    3: "restricted manoeuvrability", 4: "constrained by draught", 5: "moored",
    6: "aground", 7: "engaged in fishing", 8: "under way sailing",
    14: "AIS-SART", 15: "not defined",
}


@dataclass
class Position:
    mmsi: str
    msg_type: str
    ais_class: str
    provider_time: datetime
    lat: float
    lon: float
    sog_kn: float | None
    cog_deg: float | None
    heading_deg: float | None
    nav_status: int | None
    ais_second: int | None
    position_accuracy: int | None


@dataclass
class Static:
    mmsi: str
    msg_type: str
    provider_time: datetime
    name: str | None = None
    callsign: str | None = None
    imo: str | None = None
    ship_type: int | None = None
    length_m: float | None = None
    beam_m: float | None = None


class ParseError(ValueError):
    pass


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def clean_sog(v):
    v = _num(v)
    if v is None or v < 0 or v >= 102.2:
        return None
    return round(v, 2)


def clean_cog(v):
    v = _num(v)
    if v is None or v < 0 or v >= 360:
        return None
    return round(v, 1)


def clean_heading(v):
    v = _num(v)
    if v is None or v < 0 or v >= 360:  # 511 = not available
        return None
    return v


def clean_second(v):
    v = _num(v)
    if v is None or v < 0 or v > 59:
        return None
    return int(v)


def clean_text(v):
    if not isinstance(v, str):
        return None
    v = v.replace("@", " ").strip()
    return v or None


def valid_latlon(lat, lon) -> bool:
    return (
        lat is not None and lon is not None
        and -90 <= lat <= 90 and -180 <= lon <= 180
        and not (lat == 0 and lon == 0)
    )


def parse_message(msg: dict) -> Position | Static | None:
    """Return a Position/Static record, None for ignorable types, or raise ParseError."""
    if "error" in msg:
        raise ParseError(f"provider error: {msg['error']}")
    mtype = msg.get("MessageType")
    meta = msg.get("MetaData") or {}
    body = (msg.get("Message") or {}).get(mtype) or {}
    if mtype not in POSITION_TYPES and mtype not in STATIC_TYPES:
        return None
    mmsi = meta.get("MMSI") or body.get("UserID")
    if not mmsi:
        raise ParseError("missing MMSI")
    mmsi = str(int(mmsi)).zfill(9)
    if "time_utc" not in meta:
        raise ParseError("missing MetaData.time_utc")
    ptime = parse_aisstream_time(str(meta["time_utc"]))

    if mtype in POSITION_TYPES:
        if body.get("Valid") is False:
            raise ParseError("message flagged invalid by provider")
        lat = _num(body.get("Latitude", meta.get("latitude")))
        lon = _num(body.get("Longitude", meta.get("longitude")))
        if not valid_latlon(lat, lon):
            raise ParseError(f"invalid position {lat},{lon}")
        nav = body.get("NavigationalStatus") if mtype == "PositionReport" else None
        acc = body.get("PositionAccuracy")
        return Position(
            mmsi=mmsi, msg_type=mtype, ais_class=POSITION_TYPES[mtype], provider_time=ptime,
            lat=round(lat, 6), lon=round(lon, 6),
            sog_kn=clean_sog(body.get("Sog")), cog_deg=clean_cog(body.get("Cog")),
            heading_deg=clean_heading(body.get("TrueHeading")),
            nav_status=int(nav) if isinstance(nav, (int, float)) and not isinstance(nav, bool) else None,
            ais_second=clean_second(body.get("Timestamp")),
            position_accuracy=int(acc) if isinstance(acc, bool) else None,
        )

    st = Static(mmsi=mmsi, msg_type=mtype, provider_time=ptime)
    if mtype == "ShipStaticData":
        st.name = clean_text(body.get("Name"))
        st.callsign = clean_text(body.get("CallSign"))
        imo = body.get("ImoNumber")
        st.imo = str(imo) if imo else None
        st.ship_type = body.get("Type")
        dim = body.get("Dimension") or {}
    else:  # StaticDataReport (type 24): part A has name, part B has callsign/type/dimension
        a = body.get("ReportA") or {}
        b = body.get("ReportB") or {}
        if a.get("Valid"):
            st.name = clean_text(a.get("Name"))
        dim = {}
        if b.get("Valid"):
            st.callsign = clean_text(b.get("CallSign"))
            st.ship_type = b.get("ShipType")
            dim = b.get("Dimension") or {}
    if dim:
        a_, b_, c_, d_ = (_num(dim.get(k)) or 0 for k in "ABCD")
        st.length_m = (a_ + b_) or None
        st.beam_m = (c_ + d_) or None
    if not st.name:
        st.name = clean_text(meta.get("ShipName"))
    return st
