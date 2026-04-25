from decimal import Decimal
from typing import Self
from activity.utils import etree_to_dict_no_namespaces, haversine, format_mins_seconds
from xml.etree import ElementTree  # TODO: use defused xml or similar going forward
from datetime import datetime
import json
from activity.vendor.fitdecode import fitdecode


STREAM_NAMES = [
    "longitude",  # signed decimal degrees
    "latitude",  # signed decimal degrees
    "heart_rate",  # beats per minute
    "elevation",  # meters
    "cadence",  # per minute
    "power",  # watts
    "time",  # datetime.datetime
    "distance",  # meters
]

NORMALISED_STREAMS = {
    # gpx
    "@lon": "longitude",
    "@lat": "latitude",
    "ele": "elevation",
    "hr": "heart_rate",
    "cad": "cadence",
    "time": "time",
    "watts": "power",
    # apple watch fit
    "timestamp": "time",
    "position_lat": "latitude",
    "position_long": "longitude",
    "enhanced_altitude": "elevation",
    "heart_rate": "heart_rate",
    "power": "power",
    "cadence": "cadence",
    "distance": "distance",
}

NORMALISED_VALUES = {
    "longitude": lambda x: float(x),
    "latitude": lambda x: float(x),
    "elevation": lambda x: float(x) if x else x,
    "time": lambda x: datetime.fromisoformat(x),
    "timestamp": lambda x: datetime.fromisoformat(x),
}

LAP_FIELDS = [
    "timestamp",
    "event",
    "event_type",
    "lap_trigger",
    "start_time",
    "total_elapsed_time",
    "total_timer_time",
    "sport",
    "sub_sport",
    "wkt_step_index",
]

JSON_EXPORTABLE_FIELDS = {
    "distance": None,
    "elapsed_time": None,
    "activity_type": None,
    "elevation_gain": None,
    "elevation_loss": None,
    "start_time": str,
    "virtual": None,
    "laps": None,
}

# fields that must be loaded from the JSON document since they are not calculated
JSON_LOADABLE_FIELDS = {"virtual": None, "activity_type": None}


class Activity:
    # a dict of lists of values with normalised names
    # singular, lowercase, underscored, i.e. longitude, latitude, elevation, distance, heart_rate, power
    values_streams = {}

    # float representing total distance in kilometers
    distance = 0

    # float representing total duration in seconds, can remain zero if no timestamps in the data
    # does not account for pauses in the track
    elapsed_time = 0

    # a string representing activity type
    activity_type = ""
    virtual = False

    # floats representing total elevation gain / loss in meters
    elevation_gain = 0
    elevation_loss = 0

    bounding_box = []
    laps = []

    def __init__(
        self,
        values_streams,
        *,
        laps=[],
        distance=0,
        elapsed_time=0,
        activity_type="",
        virtual=False,
        elevation_gain=0,
        elevation_loss=0,
    ):
        self.values_streams = values_streams
        self.laps = laps
        self.distance = distance
        self.elapsed_time = elapsed_time
        self.activity_type = activity_type
        self.virtual = virtual
        self.elevation_gain = elevation_gain
        self.elevation_loss = elevation_loss

        if "time" in self.values_streams and self.values_streams["time"]:
            self.start_time = self.values_streams["time"][0]

        # The idea here is that any missing values that can be derived from the GPX file should be calculated
        if not self.distance:
            self.distance = self.calc_distance()

        if not self.elapsed_time:
            self.elapsed_time = self.calc_elapsed_time()

        if not self.elevation_gain:
            self.elevation_gain = self.calc_elevation_gain()

        if not self.elevation_loss:
            self.elevation_loss = self.calc_elevation_loss()

        # these values are always calculated
        self.bounding_box = self.calc_bounding_box()

    @classmethod
    def load(cls, fn, *, debug=False) -> Self:
        if fn.lower().endswith(".gpx"):
            with open(fn) as f:
                return Activity.load_gpx(f, debug=debug)
        elif fn.lower().endswith(".fit"):
            with open(fn, "rb") as f:
                return Activity.load_fit(f, debug=debug)

    @classmethod
    def load_fit(cls, f, *, debug=False) -> Self:
        points = []
        laps = []
        activity_type = None
        virtual = False

        with fitdecode.FitReader(
            f,
            check_crc=fitdecode.CrcCheck.DISABLED,
            error_handling=fitdecode.ErrorHandling.IGNORE,
        ) as fit:
            for frame in fit:
                if frame.frame_type == fitdecode.FIT_FRAME_DATA:
                    # filter all the way down to the "record" objects in the file
                    if frame.name == "record":
                        points.append(frame)
                    elif frame.name == "session":
                        if frame.has_field("sport"):
                            activity_type = frame.get_value("sport")

                        if frame.has_field("sub_sport"):
                            if "virtual" in str(frame.get_value("sub_sport")):
                                virtual = True
                    elif frame.name == "lap":
                        laps.append(frame)

        # iterate all the points to find the available fields
        available_fields = []
        for point in points:
            for field in point.fields:
                if field.name not in available_fields:
                    available_fields.append(field.name)

        # set start_offset to the first point that includes position data
        start_offset = 0
        if "position_lat" in available_fields:
            for p in points:
                if p.get_value("position_lat", fallback=None):
                    break
                start_offset += 1

        # it's possible that the _lat field exists but is always empty
        if start_offset == len(points):
            start_offset = 0

        streams = {}
        for k, normalised in NORMALISED_STREAMS.items():
            if k in available_fields:
                streams[normalised] = [p.get_value(k, fallback=None) for p in points[start_offset:]]

                # convert from "semicircle" to "decimal"
                if k in ["position_lat", "position_long"]:
                    streams[normalised] = [v * (180 / 2**31) if v else v for v in streams[normalised]]

        for k, fn in NORMALISED_VALUES.items():
            if k == "time":
                continue

            if k in streams:
                streams[k] = [fn(x) if x else x for x in streams[k]]

        # process laps
        # TODO: WIP, we are only going to grab the values that cannot be calculated
        activity_laps = []
        for lap in laps:
            lap_values = {}
            for field in lap.fields:
                if field.name in LAP_FIELDS:
                    val = lap.get_value(field.name)
                    if val:
                        lap_values[field.name] = val
            activity_laps.append(lap_values)

        return Activity(streams, laps=activity_laps, activity_type=activity_type, virtual=virtual)

    @classmethod
    def load_gpx(cls, f, *, debug=False) -> Self:
        gpx_string = f.read()
        tree = ElementTree.fromstring(gpx_string)
        d = etree_to_dict_no_namespaces(tree)

        points = []

        # if there are multiple track segments we are just adding all of them to points
        if type(d["gpx"]["trk"]["trkseg"]) is list:
            print(
                "[WARNING] Track segments will be merged. This is not supported well! Number of segments: ",
                len(d["gpx"]["trk"]["trkseg"]),
            )
            for seg in d["gpx"]["trk"]["trkseg"]:
                if seg and "trkpt" in seg:
                    for pt in seg["trkpt"]:
                        points.append(pt)
        else:
            points = d["gpx"]["trk"]["trkseg"]["trkpt"]

        # We are effectively using the first GPX point to determine the available datasets.
        # This probably doesn't work well! Especially elevation/heart rate can exist in the file but not at the start of the track.
        # I'm unsure whether there will be empty keys for that dataset, or if the key will simple be missing from the point.
        streams = {}
        for k, normalised in NORMALISED_STREAMS.items():
            if k in points[0]:
                streams[normalised] = [p.get(k) for p in points]
            elif (
                "extensions" in points[0]
                and "TrackPointExtension" in points[0]["extensions"]
                and k in points[0]["extensions"]["TrackPointExtension"].keys()
            ):
                try:
                    streams[normalised] = [p["extensions"]["TrackPointExtension"].get(k) for p in points]
                except KeyError:
                    print(f"[WARNING]: point missing extensions ({k})")

        # normalise the values in the streams
        for k, fn in NORMALISED_VALUES.items():
            if k in streams:
                streams[k] = [fn(x) if x else x for x in streams[k]]

        # this section is just debugging, outputs keys that are missing from our streams
        if True:
            all_keys = list(points[0].keys())
            if "extensions" in points[0]:
                all_keys.extend(list(points[0]["extensions"]["TrackPointExtension"].keys()))

            ignored_keys = ["extensions"]  # keys that we won't complain about
            missing_keys = [x for x in all_keys if x not in NORMALISED_STREAMS.keys() and x not in ignored_keys]
            if missing_keys and debug:
                print("[DEBUG] Keys in file were not processed: ", missing_keys)

        return Activity(streams)

    @classmethod
    def load_json(cls, j, *, debug=False) -> Self:
        streams = {}
        extra_args = {}

        for k, v in j.items():
            if k.endswith("_stream"):
                stream_name = k.replace("_stream", "")
                if stream_name in STREAM_NAMES:
                    streams[stream_name] = v

            if k in JSON_LOADABLE_FIELDS:
                fn = JSON_LOADABLE_FIELDS[k]
                if fn:
                    extra_args[k] = fn(v)
                else:
                    extra_args[k] = v

        # normalise the values in the streams
        for k, fn in NORMALISED_VALUES.items():
            if k in streams:
                streams[k] = [fn(x) if x else x for x in streams[k]]

        return Activity(streams, **extra_args)

    def as_json(self, include_streams=True, indent=None) -> str:
        result = {}

        for k, fn in JSON_EXPORTABLE_FIELDS.items():
            if not fn:
                result[k] = getattr(self, k, None)
            else:
                val = getattr(self, k, None)
                if val:
                    result[k] = fn(val)
                else:
                    result[k] = val

        if include_streams:
            for k in STREAM_NAMES:
                result[f"{k}_stream"] = self.values_streams[k] if k in self.values_streams else []

        return json.dumps(result, default=str, indent=indent)

    def as_geojson(self):
        """
        Outputs a GeoJSON compatible JSON object

        Simply extracts the longitude and latitudes as a LineString (with no accounting for pauses/gaps in the track)
        """
        if all([x in self.values_streams for x in ["latitude", "longitude", "time"]]):
            return json.dumps(
                {
                    "type": "Feature",
                    "properties": {
                        "activity": "run",
                        "start_time": self.values_streams["time"][0].isoformat(),
                    },
                    "geometry": {
                        "type": "LineString",
                        "coordinates": [
                            [lon, lat]
                            for lat, lon in zip(
                                self.values_streams["latitude"],
                                self.values_streams["longitude"],
                            )
                            if lat and lon
                        ],
                    },
                }
            )

    def as_gpx_track(self):
        """
        Outputs a GPX file containing the route

        This will only include the latitude, longitude and elevation streams.
        """
        if not all([x in self.values_streams for x in ["latitude", "longitude"]]):
            return None

        gpx = ElementTree.Element(
            "gpx",
            {
                "version": "1.1",
                "creator": "https://github.com/sesh/activity",
                "xmlns": "http://www.topografix.com/GPX/1/1",
                "xmlns:xsi": "http://www.w3.org/2001/XMLSchema-instance",
                "xsi:schemaLocation": "http://www.topografix.com/GPX/1/1 http://www.topografix.com/GPX/1/1/gpx.xsd",
            },
        )

        trk = ElementTree.SubElement(gpx, "trk")
        trkseg = ElementTree.SubElement(trk, "trkseg")

        latitudes = self.values_streams["latitude"]
        longitudes = self.values_streams["longitude"]
        elevations = self.values_streams["elevation"] if "elevation" in self.values_streams else [None] * len(latitudes)

        for lat, lon, ele in zip(
            latitudes,
            longitudes,
            elevations,
        ):
            if lat is not None and lon is not None:
                trkpt = ElementTree.SubElement(trkseg, "trkpt", {"lat": str(lat), "lon": str(lon)})

                # Add elevation if available
                if ele is not None:
                    ele_elem = ElementTree.SubElement(trkpt, "ele")
                    ele_elem.text = str(ele)

        return ElementTree.tostring(gpx, encoding="unicode", xml_declaration=True)

    def streams(self):
        return list(self.values_streams.keys())

    """
    calc_* functions should check that they have the streams needed for the calculations first, and return
    the defined default value if there is insufficient data.

    All calc_* functions should take a start_index and end_index, if they are set, then the value should be 
    calculated for that range only.
    """

    def calc_bounding_box(self, start_index=None, end_index=None):
        if not all([x in self.values_streams for x in ["latitude", "longitude"]]):
            return []

        latitudes = [x for x in self.values_streams["latitude"] if x]
        longitudes = [x for x in self.values_streams["longitude"] if x]

        if not latitudes or not longitudes:
            return None

        if start_index and end_index:
            latitudes = latitudes[start_index:end_index]
            longitudes = longitudes[start_index:end_index]

        min_lat, max_lat = min(latitudes), max(latitudes)
        min_lon, max_lon = min(longitudes), max(longitudes)

        return [[min_lon, min_lat], [max_lon, max_lat]]

    def calc_distance(self, start_index=None, end_index=None):
        # returns in km
        distance = 0

        if all([x in self.values_streams for x in ["latitude", "longitude"]]):
            points = list(zip(self.values_streams["latitude"], self.values_streams["longitude"]))
            if start_index and end_index:
                points = points[start_index:end_index]

            prev_pt = None

            for lat, lon in points:
                # support points with missing values
                if prev_pt and prev_pt[0] and prev_pt[1] and lat and lon:
                    distance += haversine((lat, lon), prev_pt)
                prev_pt = (lat, lon)

        if distance == 0 and "distance" in self.values_streams and len(self.values_streams["distance"]) > 0:
            # use the pre-calculated distance stream if we didn't calculate one
            # convert from m to km
            return self.values_streams["distance"][-1] / 1000

        return distance

    def calc_distance_values(self, start_index=None, end_index=None):
        values = [0]
        if all([x in self.values_streams for x in ["latitude", "longitude"]]):
            positions = [x for x in zip(self.values_streams["latitude"], self.values_streams["longitude"])]

            if start_index and end_index:
                positions = positions[start_index:end_index]

            for prev, pos in zip(positions, positions[1:]):
                dist = haversine(prev, pos)
                values.append(values[-1] + dist)

        return values

    def calc_elapsed_time(self, start_index=None, end_index=None):
        if "time" in self.values_streams and self.values_streams["time"]:
            if start_index and end_index:
                return (self.values_streams["time"][end_index] - self.values_streams["time"][start_index]).total_seconds()

            return (self.values_streams["time"][-1] - self.values_streams["time"][0]).total_seconds()

        return 0

    def calc_elevation_gain(self, start_index=None, end_index=None):
        elevation_gain = 0
        if "elevation" in self.values_streams:
            points = list(
                zip(
                    self.values_streams["elevation"],
                    self.values_streams["elevation"][1:],
                )
            )
            if start_index and end_index:
                points = points[start_index:end_index]

            for previous_point, point in points:
                if point and previous_point and point > previous_point:
                    elevation_gain += point - previous_point

        return elevation_gain

    def calc_elevation_loss(self, start_index=None, end_index=None):
        elevation_loss = 0
        if "elevation" in self.values_streams:
            points = list(
                zip(
                    self.values_streams["elevation"],
                    self.values_streams["elevation"][1:],
                )
            )
            if start_index and end_index:
                points = points[start_index:end_index]

            for previous_point, point in points:
                if point and previous_point and point < previous_point:
                    elevation_loss -= point - previous_point

        return elevation_loss

    def calc_moving_time(self, *, threshold_m=0.79, start_index=None, end_index=None):
        if not all([x in self.values_streams for x in ["longitude", "latitude", "time"]]):
            return self.calc_elapsed_time()

        prev = None
        stationary_seconds = 0

        points = list(
            zip(
                self.values_streams["latitude"],
                self.values_streams["longitude"],
                self.values_streams["time"],
            )
        )

        if start_index and end_index:
            points = points[start_index:end_index]

        for point in points:
            if prev and point[0] and point[1]:
                dist_km = haversine(prev, point)
                dist_m = dist_km * 1000

                if dist_m < threshold_m:
                    stationary_seconds += (point[2] - prev[2]).total_seconds()

            if point[0] and point[1]:
                prev = point

        return self.calc_elapsed_time(start_index=start_index, end_index=end_index) - stationary_seconds

    def calc_time_in_zone(self, zones: list[int], stream: str, start_index=None, end_index=None):
        """
        Provide zones as the top of the range for the zone with a high last value, i.e. for HR: [128, 146, 166, 180, 999]
        """
        zone_time_seconds = [0 for _ in range(len(zones))]

        # special case for this calculated stream
        if stream == "pace":
            values_stream = self.calc_pace_values()

        else:
            if stream not in self.values_streams:
                return zone_time_seconds

            values_stream = self.values_streams[stream]

        points = list(zip(self.values_streams["time"], values_stream))

        if start_index and end_index:
            points = points[start_index:end_index]

        prev_dt = None
        prev_val = None
        for dt, val in points:
            elapsed = (dt - prev_dt).total_seconds() if prev_dt else 0

            # if we have neither prev_val or val exit
            if not val and not prev_val:
                prev_dt = dt
                continue

            # if we are missing val, but have a prev_val use that
            if not val:
                val = prev_val

            for i, z in enumerate(zones):
                # flip between lte and gte based on the direction of the zones
                if zones[0] < zones[-1]:
                    if val <= z:
                        zone_time_seconds[i] += elapsed
                        break
                else:
                    if val >= z:
                        zone_time_seconds[i] += elapsed
                        break

            prev_dt = dt
            prev_val = val

        return zone_time_seconds

    def calc_splits(self, split: Decimal = Decimal("1.0"), start_index=None, end_index=None) -> list[int]:
        if not all([x in self.values_streams for x in ["longitude", "latitude", "time"]]):
            return []

        splits = []
        next_split = split
        split_start = self.values_streams["time"][0]
        distance = 0
        prev = None

        points = list(
            zip(
                self.values_streams["latitude"],
                self.values_streams["longitude"],
                self.values_streams["time"],
            )
        )

        if start_index and end_index:
            points = points[start_index:end_index]

        for lat, lon, clock in points:
            if lat is None or lon is None:
                continue

            if prev:
                distance += haversine(prev, (lat, lon))
                if distance >= next_split:
                    splits.append((clock - split_start).total_seconds())
                    split_start = clock
                    next_split += split

            prev = (lat, lon)

        return splits

    def calc_pace(self, start_index=None, end_index=None):
        t = self.calc_elapsed_time(start_index=start_index, end_index=end_index)
        d = self.calc_distance(start_index=start_index, end_index=end_index)

        if not t or not d:
            return 0

        return t / d

    def calc_moving_pace(self, start_index=None, end_index=None):
        t = self.calc_moving_time(start_index=start_index, end_index=end_index)
        d = self.calc_distance(start_index=start_index, end_index=end_index)

        if not t or not d:
            return 0

        return t / d

    def calc_pace_values(self, start_index=None, end_index=None):
        if not all([x in self.values_streams for x in ["longitude", "latitude", "time"]]):
            return []

        prev = None
        pace_values = []
        points = list(
            zip(
                self.values_streams["latitude"],
                self.values_streams["longitude"],
                self.values_streams["time"],
            )
        )

        if start_index and end_index:
            points = points[start_index:end_index]

        for lat, lon, clock in points:
            if lat is None or lon is None:
                pace_values.append(None)
                continue

            if prev:
                t = (clock - prev[2]).total_seconds()
                d = haversine((prev[0], prev[1]), (lat, lon))

                if not t or not d:
                    pace_values.append(0)
                else:
                    pace_values.append(t / d)
            else:
                pace_values.append(None)

            prev = (lat, lon, clock)

        return pace_values

    def calc_grade_values(self, start_index=None, end_index=None):
        if not all([x in self.values_streams for x in ["longitude", "latitude", "elevation"]]):
            return []

        prev = None
        grade_values = []
        points = list(
            zip(
                self.values_streams["latitude"],
                self.values_streams["longitude"],
                self.values_streams["elevation"],
            )
        )

        if start_index and end_index:
            points = points[start_index:end_index]

        for lat, lon, elev in points:
            if lat is None or lon is None or elev is None:
                grade_values.append(None)
                continue

            if prev:
                # Calculate horizontal distance in meters (haversine returns km)
                horizontal_distance_m = haversine((prev[0], prev[1]), (lat, lon)) * 1000
                # Calculate elevation change in meters
                elevation_change_m = elev - prev[2]

                if horizontal_distance_m > 0:
                    # Grade as percentage: (rise / run) * 100
                    grade = (elevation_change_m / horizontal_distance_m) * 100
                    grade_values.append(grade)
                else:
                    grade_values.append(0)
            else:
                grade_values.append(None)

            prev = (lat, lon, elev)

        return grade_values

    def calc_windowed_pace(self, window=5, start_index=None, end_index=None):
        time_values = self.values_streams["time"]
        pace_values = self.calc_pace_values()

        if start_index and end_index:
            time_values = time_values[start_index:end_index]
            pace_values = pace_values[start_index:end_index]

        return self._calc_stream_windowed_average(time_values, pace_values, window)

    def calc_average_heart_rate(self, start_index=None, end_index=None):
        return self._calc_stream_average("heart_rate", start_index, end_index)

    def calc_average_power(self, start_index=None, end_index=None):
        return self._calc_stream_average("power", start_index, end_index)

    def calc_windowed_power(self, window=30, start_index=None, end_index=None):
        time_values = self.values_streams["time"]
        power_values = self.values_streams["power"]

        if start_index and end_index:
            time_values = time_values[start_index:end_index]
            power_values = power_values[start_index:end_index]

        return self._calc_stream_windowed_average(time_values, power_values, window)

    def calc_clock_values(self, start_index=None, end_index=None):
        if "time" not in self.values_streams:
            return []

        points = [x for x in self.values_streams["time"] if x]
        if start_index and end_index:
            points = points[start_index:end_index]

        if not points:
            return []

        start = points[0]
        return [(x - start).total_seconds() for x in points]

    def _calc_stream_average(self, stream_name, start_index=None, end_index=None):
        if stream_name not in self.values_streams:
            return 0

        points = [x for x in self.values_streams[stream_name] if x]
        if start_index and end_index:
            points = points[start_index:end_index]

        if not points:
            return 0

        return sum(points) / len(points)

    def _calc_stream_windowed_average(self, time_values, stream_values, window_seconds=30):
        # this is just an averaged window
        # uplot has examples of other smoothing options:
        # https://leeoniya.github.io/uPlot/demos/data-smoothing.html
        result = []

        start_time = time_values[0]
        times = [(t - start_time).total_seconds() for t in time_values]
        window_start_idx = 0

        for i in range(len(times)):
            current_time = times[i]
            window_min_time = current_time - window_seconds

            while window_start_idx < i and times[window_start_idx] < window_min_time:
                window_start_idx += 1

            # strip out missing values
            window_values = [x for x in stream_values[window_start_idx : i + 1] if x]
            if len(window_values):
                avg = sum(window_values) / len(window_values)
            else:
                avg = None
            result.append(avg)

        return result

    """
    Utilities functions
    """

    def distance_between_points(self, start_index, end_index):
        distance = 0.0
        prev = None
        for point in zip(
            self.values_streams["latitude"][start_index : end_index + 1],
            self.values_streams["longitude"][start_index : end_index + 1],
        ):
            if prev:
                distance += haversine(point, prev)
            prev = point
        return distance

    def elevation_change_between_points(self, start_index, end_index):
        elevation_change = 0.0
        for prev, point in zip(
            self.values_streams["elevation"][start_index : end_index + 1],
            self.values_streams["elevation"][start_index + 1 : end_index + 1],
        ):
            elevation_change += point - prev

        return elevation_change

    def index_at_distance(self, dist_km):
        """
        Returns the first index where distance is > dist_km
        """
        distance = 0.0
        i = 0
        prev = None

        for point in zip(
            self.values_streams["latitude"],
            self.values_streams["longitude"],
        ):
            i += 1
            if prev and prev[0] and prev[1] and point[0] and point[1]:
                distance += haversine(prev, point)

                if distance > dist_km:
                    return i

            prev = point

    def index_at_time(self, time_seconds):
        start = self.values_streams["time"][0]

        for i, dt in enumerate(self.values_streams["time"]):
            if (dt - start).total_seconds() >= time_seconds:
                return i
        return 0

    def clock_at_distance(self, dist_km):
        distance = 0.0
        i = 0
        prev = None

        for point in zip(
            self.values_streams["latitude"],
            self.values_streams["longitude"],
            self.values_streams["time"],
        ):
            i += 1
            if prev:
                distance += haversine(prev, point[:2])

                if distance > dist_km:
                    return (point[2] - self.values_streams["time"][0]).total_seconds()
            prev = point[:2]
