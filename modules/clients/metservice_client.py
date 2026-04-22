#!/usr/bin/env python3
"""Helpers for retrieving NZ forecasts from MetService's public web JSON."""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List, Optional

import requests


BASE_URL = "https://www.metservice.com"

# Common region aliases from geocoders -> MetService path slugs.
REGION_ALIASES = {
    "auckland": "auckland",
    "bay of plenty": "bay-of-plenty",
    "canterbury": "canterbury",
    "coromandel": "coromandel",
    "gisborne": "gisborne",
    "hawke's bay": "hawkes-bay",
    "hawkes bay": "hawkes-bay",
    "manawatu-whanganui": "manawatu-whanganui",
    "manawatū-whanganui": "manawatu-whanganui",
    "marlborough": "marlborough",
    "nelson": "nelson",
    "northland": "northland",
    "otago": "otago",
    "southland": "southland",
    "taranaki": "taranaki",
    "tasman": "tasman",
    "taupo": "taupo",
    "taupō": "taupo",
    "waikato": "waikato",
    "wairarapa": "wairarapa",
    "wellington": "wellington",
    "west coast": "west-coast",
    "westcoast": "west-coast",
}


def _slugify(value: str) -> str:
    """Convert a location or region name into a MetService-style slug."""
    normalized = unicodedata.normalize("NFKD", value or "")
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    ascii_text = ascii_text.lower()
    ascii_text = ascii_text.replace("&", " and ")
    ascii_text = re.sub(r"[^a-z0-9]+", "-", ascii_text)
    ascii_text = re.sub(r"-{2,}", "-", ascii_text)
    return ascii_text.strip("-")


def _normalize_region(value: Optional[str]) -> Optional[str]:
    """Map a geocoder region/state label to a MetService region slug."""
    if not value:
        return None
    simplified = value.strip().lower()
    if simplified in REGION_ALIASES:
        return REGION_ALIASES[simplified]
    return _slugify(value)


def _normalize_location_name(value: Optional[str]) -> Optional[str]:
    """Normalize a place label for MetService path lookup."""
    if not value:
        return None
    lowered = value.strip().lower()
    for suffix in (
        " city",
        " district",
        " region",
        " area",
        " airport",
        " aerodrome",
        " township",
        " suburb",
    ):
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return _slugify(lowered)


def build_metservice_path_candidates(location: str, address_info: Optional[Dict[str, Any]] = None) -> List[str]:
    """Build likely MetService public-data paths from a location/address."""
    address_info = address_info or {}
    names: List[str] = []
    for field in (
        "city",
        "town",
        "village",
        "municipality",
        "suburb",
        "hamlet",
        "county",
    ):
        value = address_info.get(field)
        if value:
            names.append(value)
    names.append(location)

    name_slugs = []
    for value in names:
        slug = _normalize_location_name(value)
        if slug and slug not in name_slugs:
            name_slugs.append(slug)

    regions: List[str] = []
    for field in ("state", "region", "state_district", "province"):
        region_slug = _normalize_region(address_info.get(field))
        if region_slug and region_slug not in regions:
            regions.append(region_slug)

    candidates: List[str] = []
    for region_slug in regions:
        for name_slug in name_slugs:
            for section in ("towns-cities", "rural"):
                path = f"/{section}/regions/{region_slug}/locations/{name_slug}"
                if path not in candidates:
                    candidates.append(path)
    return candidates


def fetch_json(url: str, session: Optional[requests.Session] = None, timeout: int = 10) -> Dict[str, Any]:
    """Fetch JSON from MetService, raising for HTTP errors."""
    client = session or requests
    response = client.get(
        url,
        timeout=timeout,
        headers={
            "Accept-Encoding": "gzip",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36"
            ),
        },
    )
    response.raise_for_status()
    return response.json()


def _find_first_data_url(obj: Any, needle: str) -> Optional[str]:
    """Find the first nested `dataUrl` containing a substring."""
    if isinstance(obj, dict):
        data_url = obj.get("dataUrl")
        if isinstance(data_url, str) and needle in data_url:
            return data_url
        for value in obj.values():
            result = _find_first_data_url(value, needle)
            if result:
                return result
    elif isinstance(obj, list):
        for value in obj:
            result = _find_first_data_url(value, needle)
            if result:
                return result
    return None


def fetch_metservice_public_weather(
    location_path: str,
    session: Optional[requests.Session] = None,
    timeout: int = 10,
) -> Dict[str, Any]:
    """Fetch the MetService public weather payloads we need for wx/service."""
    normalized_path = location_path if location_path.startswith("/") else f"/{location_path}"
    current_shell = fetch_json(f"{BASE_URL}/publicData/webdata{normalized_path}", session=session, timeout=timeout)
    daily_shell = fetch_json(
        f"{BASE_URL}/publicData/webdata{normalized_path}/7-days", session=session, timeout=timeout
    )

    current_conditions_url = _find_first_data_url(current_shell, "/currentConditions/")
    graph_url = _find_first_data_url(current_shell, "/48hourGraph/")
    two_day_url = _find_first_data_url(current_shell, "/twoDayForecast/")
    seven_day_url = _find_first_data_url(daily_shell, "/sevenDayForecast/")
    warnings_path = ((current_shell.get("location") or {}).get("warningDataUrl")) or ""

    if not current_conditions_url or not seven_day_url:
        raise ValueError("MetService response missing forecast modules")

    result = {
        "location": current_shell.get("location") or {},
        "current_conditions": fetch_json(f"{BASE_URL}{current_conditions_url}", session=session, timeout=timeout),
        "daily_forecast": fetch_json(f"{BASE_URL}{seven_day_url}", session=session, timeout=timeout),
        "graph": fetch_json(f"{BASE_URL}{graph_url}", session=session, timeout=timeout) if graph_url else {},
        "two_day_forecast": (
            fetch_json(f"{BASE_URL}{two_day_url}", session=session, timeout=timeout) if two_day_url else {}
        ),
        "warnings": fetch_json(f"{BASE_URL}{warnings_path}", session=session, timeout=timeout) if warnings_path else {},
        "location_path": normalized_path,
    }
    return result

