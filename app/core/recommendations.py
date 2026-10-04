"""Recommendation rules shared by the service and in-memory repository."""
from math import asin, cos, radians, sin, sqrt


def distance_km(origin, destination):
    if not origin or not destination or origin.latitud is None or destination.latitud is None:
        return None
    lat1, lat2 = radians(origin.latitud), radians(destination.latitud)
    delta_lat = lat2 - lat1
    delta_lon = radians(destination.longitud - origin.longitud)
    value = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    return 12742 * asin(sqrt(max(0, min(1, value))))


def compatibility(mine, theirs):
    my_levels = {s.deporte_codigo.lower(): s.nivel for s in mine}
    their_levels = {s.deporte_codigo.lower(): s.nivel for s in theirs}
    union = my_levels.keys() | their_levels.keys()
    if not union:
        return 0
    weight = sum(1 - abs(my_levels[code] - their_levels[code]) / 4
                 for code in my_levels.keys() & their_levels.keys())
    return round(100 * weight / len(union))


def similar_level(mine, theirs):
    return any(a.deporte_codigo.lower() == b.deporte_codigo.lower() and abs(a.nivel - b.nivel) <= 1
               for a in mine for b in theirs)


def matches_filters(theirs, mine, distance, filters):
    if filters.radius_km is not None and (distance is None or distance > filters.radius_km):
        return False
    restrict_sport = (filters.sport is not None or filters.shared_sports or filters.level_tolerance is not None
                      or filters.min_level != 1 or filters.max_level != 5)
    if not restrict_sport:
        return True
    levels = {sport.deporte_codigo.lower(): sport.nivel for sport in mine}
    return any(
        (filters.sport is None or s.deporte_codigo.lower() == filters.sport.lower())
        and filters.min_level <= s.nivel <= filters.max_level
        and (not filters.shared_sports or s.deporte_codigo.lower() in levels)
        and (filters.level_tolerance is None or (
            s.deporte_codigo.lower() in levels
            and abs(s.nivel - levels[s.deporte_codigo.lower()]) <= filters.level_tolerance))
        for s in theirs
    )
