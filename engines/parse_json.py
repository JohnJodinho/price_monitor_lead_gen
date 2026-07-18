import json
from pathlib import Path
from typing import Any


def _load_json_source(source: Any):
    if isinstance(source, (str, Path)):
        with open(source, encoding="utf-8") as file:
            return json.load(file)
    if isinstance(source, dict):
        return source
    raise TypeError("parse_json expects a file path or a parsed dictionary")


def parse_json(source, structured=False):
    data = _load_json_source(source)

    latitude = None
    longitude = None
    host = None
    rating = None
    review_count = None

    root_key = None
    if "niobeMinimalClientData" in data:
        root_key = "niobeMinimalClientData"
    elif "niobeClientData" in data:
        root_key = "niobeClientData"

    sections = None
    node_data = None
    if root_key:
        try:
            root_payload = data[root_key][0][1]["data"]
        except Exception:
            root_payload = None

        if root_payload:
            try:
                sections = root_payload["presentation"]["stayProductDetailPage"]["sections"]
            except Exception:
                sections = None

            try:
                node_data = root_payload["node"]
            except Exception:
                node_data = None

    if sections:
        try:
            latitude = sections["metadata"]["loggingContext"]["eventDataLogging"]["listingLat"]
        except Exception:
            try:
                latitude = sections[1]["section"]["lat"]
            except Exception:
                latitude = None

        try:
            longitude = sections["metadata"]["loggingContext"]["eventDataLogging"]["listingLng"]
        except Exception:
            try:
                longitude = sections[1]["section"]["lng"]
            except Exception:
                longitude = None

        try:
            within_sections = sections["sections"]
            for section in within_sections:
                try:
                    host = section["section"]["cardData"]["name"]
                    break
                except Exception:
                    continue
        except Exception:
            host = None

    if sections:
        try:
            rating = sections["metadata"]["sharingConfig"]["starRating"]
        except Exception:
            rating = None
        if rating is None:
            try:
                rating = sections["sbuiData"]["sectionConfiguration"]["root"]["sections"][0]["sectionData"]["reviewData"]["ratingText"]
            except Exception:
                rating = None
        if rating is None:
            try:
                rating = sections["sections"]["sections"][0]["section"]["overallRating"]
            except Exception:
                rating = None

    if sections:
        try:
            review_count = sections["metadata"]["sharingConfig"]["reviewCount"]
        except Exception:
            review_count = None

    if review_count is None and node_data:
        try:
            review_count = node_data["sharingConfig"]["reviewCount"]
        except Exception:
            review_count = None

    if structured:
        return {
            "root_key": root_key,
            "sections": sections,
            "raw_root": data.get(root_key) if root_key else data,
            "review_count": review_count,
        }

    return {
        "latitude": latitude,
        "longitude": longitude,
        "host": host,
        "rating": rating,
        "review_count": review_count,
    }
