from collections import defaultdict
from math import asin, cos, radians, sin, sqrt, pow

AVG_EARTH_RADIUS = 6371  # in km
GPXPY_EARTH_RADIUS = 6378.137 * 1000


def haversine(point1, point2, *, gpxpy=False) -> float:
    if gpxpy:
        return haversine_gpxpy(point1[0], point1[1], point2[0], point2[1]) / 1000
    else:
        return haversine_old(point1, point2)


def haversine_gpxpy(latitude_1: float, longitude_1: float, latitude_2: float, longitude_2: float) -> float:
    """
    https://github.com/tkrajina/gpxpy/blob/09fc46b3cad16b5bf49edf8e7ae873794a959620/gpxpy/geo.py#L34-L50

    Haversine distance between two points, expressed in meters.

    Implemented from http://www.movable-type.co.uk/scripts/latlong.html
    """
    d_lon = radians(longitude_1 - longitude_2)
    lat1 = radians(latitude_1)
    lat2 = radians(latitude_2)
    d_lat = lat1 - lat2

    a = pow(sin(d_lat / 2), 2) + pow(sin(d_lon / 2), 2) * cos(lat1) * cos(lat2)
    c = 2 * asin(sqrt(a))
    d = GPXPY_EARTH_RADIUS * c

    return d


def haversine_old(point1, point2):
    """
    Calculate the great-circle distance between two points on the Earth surface.
    :input: two 2-tuples, containing the latitude and longitude of each point
    in decimal degrees.
    Example: haversine((45.7597, 4.8422), (48.8567, 2.3508))
    :output: Returns the distance between the two points in kilometers
    """
    # unpack latitude/longitude
    lat1, lng1 = point1[0], point1[1]
    lat2, lng2 = point2[0], point2[1]

    # convert all latitudes/longitudes from decimal degrees to radians
    lat1, lng1, lat2, lng2 = map(radians, (lat1, lng1, lat2, lng2))

    # calculate haversine
    lat = lat2 - lat1
    lng = lng2 - lng1
    d = sin(lat * 0.5) ** 2 + cos(lat1) * cos(lat2) * sin(lng * 0.5) ** 2
    h = 2 * AVG_EARTH_RADIUS * asin(sqrt(d))

    return h  # in kilometers


def format_mins_seconds(d):
    d = int(d)
    minutes, seconds = divmod(d, 60)

    if minutes < 60:
        return f"00:{minutes:02}:{seconds:02}"

    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def format_mins_seconds_lstrip(d):
    s = format_mins_seconds(d)
    if s.startswith("00:"):
        s = s[3:]

    if s.startswith("0"):
        s = s[1:]

    return s


def duration_to_seconds(s):
    # HH:MM:SS to x
    parts = s.split(":")
    if len(parts) == 3:
        return (float(parts[0]) * 60 * 60) + (float(parts[1]) * 60) + float(parts[2])
    elif len(parts) == 2:
        return (float(parts[0]) * 60) + float(parts[1])
    elif len(parts) == 1:
        return parts[0]


def semicircle_to_degrees(semicircles):
    """Convert a number in semicricles to degrees"""
    return semicircles * (180.0 / 2.0**31)


def etree_to_dict_no_namespaces(t):
    tag = t.tag.split("}")[-1]
    d = {tag: {} if t.attrib else None}

    children = list(t)
    if children:
        dd = defaultdict(list)
        for dc in map(etree_to_dict_no_namespaces, children):
            for k, v in dc.items():
                dd[k.split("}")[-1]].append(v)
        d = {tag: {k.split("}")[-1]: v[0] if len(v) == 1 else v for k, v in dd.items()}}
    if t.attrib:
        d[tag].update(("@" + k, v) for k, v in t.attrib.items())
    if t.text:
        text = t.text.strip()
        if children or t.attrib:
            if text:
                d[tag]["#text"] = text
        else:
            d[tag] = text
    return d


def parse_args(args):
    result = {
        a.split("=")[0]: (int(a.split("=")[1]) if "=" in a and a.split("=")[1].isnumeric() else a.split("=")[1] if "=" in a else True)
        for a in args
        if "--" in a
    }
    result["[]"] = [a for a in args if not a.startswith("--")]
    return result


def filter_similar_values(values, key_values=[], min_diff=10):
    # remove similar values
    filtered_values = []
    i = 0

    while i < len(values):
        if i == 0 or i == len(values) - 1:
            # always include the first and last marker
            filtered_values.append(values[i])
        elif values[i + 1] - values[i] < min_diff:
            # if they are close, prefer named values, otherwise the latter one
            if values[i] in key_values:
                filtered_values.append(values[i])
            else:
                filtered_values.append(values[i + 1])
            i += 1  # skip an extra marker
        else:
            filtered_values.append(values[i])

        i += 1

    return filtered_values
