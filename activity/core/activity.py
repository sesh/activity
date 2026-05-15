from decimal import Decimal
from bisect import bisect_left, bisect_right
from typing import Self
from activity.utils import etree_to_dict_no_namespaces, haversine, format_mins_seconds
from xml.etree import ElementTree  # TODO: use defused xml or similar going forward
from datetime import datetime
import json
from activity.vendor.fitdecode import fitdecode

GRADE_ADJUSTED_PACE_BASE_COST = 3.6
GRADE_ADJUSTED_PACE_GRADE_SMOOTHING_SECONDS = 3


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
    "heart_rate": lambda x: float(x) if x else x,
    "power": lambda x: float(x) if x else x,
    "cadence": lambda x: float(x) if x else x,
    "distance": lambda x: float(x) if x else x,
    "time": lambda x: datetime.fromisoformat(x),
    "timestamp": lambda x: datetime.fromisoformat(x),
    "segments": lambda xs: (values := [datetime.fromisoformat(x) if isinstance(x, str) else x for x in xs])[: len(values) // 2 * 2],
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
    "segments": None,
}

# fields that must be loaded from the JSON document since they are not calculated
JSON_LOADABLE_FIELDS = {"virtual": None, "activity_type": None, "segments": NORMALISED_VALUES["segments"]}


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
    segments = []

    def __init__(
        self,
        values_streams,
        *,
        laps=[],
        segments=[],
        distance=0,
        elapsed_time=0,
        activity_type="",
        virtual=False,
        elevation_gain=0,
        elevation_loss=0,
    ):
        self.values_streams = values_streams
        self.laps = laps
        self.segments = segments
        self.distance = distance
        self.elapsed_time = elapsed_time
        self.activity_type = activity_type
        self.virtual = virtual
        self.elevation_gain = elevation_gain
        self.elevation_loss = elevation_loss

        if "time" in self.values_streams and self.values_streams["time"]:
            self.start_time = self.values_streams["time"][0]
            if not self.segments:
                self.segments = [self.values_streams["time"][0], self.values_streams["time"][-1]]

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
        events = []
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
                    elif frame.name == "event":
                        events.append(frame)

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

        segments = NORMALISED_VALUES["segments"](list(cls._extract_segments_from_fit_events(events, streams.get("time", []))))

        return Activity(streams, laps=activity_laps, segments=segments, activity_type=activity_type, virtual=virtual)

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
                result[k] = self._serialize_segment_timestamps() if k == "segments" else getattr(self, k, None)
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

    def _stream_length(self):
        if "time" in self.values_streams:
            return len(self.values_streams["time"])

        if self.values_streams:
            return len(next(iter(self.values_streams.values())))

        return 0

    def _active_index_ranges(self, start_index=None, end_index=None):
        stream_length = self._stream_length()
        if stream_length == 0:
            return []

        range_start = 0 if start_index is None else max(0, start_index)
        range_end = stream_length if end_index is None else min(end_index, stream_length)
        if range_end <= range_start:
            return []

        if "time" not in self.values_streams or not self.values_streams["time"] or not self.segments:
            return [(range_start, range_end)]

        times = self.values_streams["time"]
        ranges = []
        for segment_start, segment_end in zip(self.segments[::2], self.segments[1::2]):
            segment_start_index = bisect_left(times, segment_start)
            segment_end_index = bisect_right(times, segment_end)
            clipped_start = max(range_start, segment_start_index)
            clipped_end = min(range_end, segment_end_index)
            if clipped_end > clipped_start:
                ranges.append((clipped_start, clipped_end))

        return ranges

    def _active_stream(self, stream_name, start_index=None, end_index=None):
        if stream_name not in self.values_streams:
            return []

        values = self.values_streams[stream_name]
        result = []
        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            result.extend(values[range_start:range_end])

        return result

    def _active_stream_by_range(self, stream_name, start_index=None, end_index=None):
        if stream_name not in self.values_streams:
            return []

        values = self.values_streams[stream_name]
        return [
            values[range_start:range_end]
            for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index)
        ]

    def _iter_active_points(self, *stream_names, start_index=None, end_index=None):
        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            for idx in range(range_start, range_end):
                yield tuple(
                    self.values_streams[stream_name][idx] if idx < len(self.values_streams[stream_name]) else None
                    for stream_name in stream_names
                )

    def _iter_active_adjacent_points(self, *stream_names, start_index=None, end_index=None):
        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            prev = None
            for idx in range(range_start, range_end):
                point = tuple(
                    self.values_streams[stream_name][idx] if idx < len(self.values_streams[stream_name]) else None
                    for stream_name in stream_names
                )
                if prev is not None:
                    yield prev, point
                prev = point

    """
    calc_* functions should check that they have the streams needed for the calculations first, and return
    the defined default value if there is insufficient data.

    All calc_* functions should take a start_index and end_index, if they are set, then the value should be
    calculated for that range only.
    """

    def calc_bounding_box(self, start_index=None, end_index=None):
        # [[min_lon, min_lat], [max_lon, max_lat]] in degrees
        if not all([x in self.values_streams for x in ["latitude", "longitude"]]):
            return []

        points = [
            (lat, lon)
            for lat, lon in self._iter_active_points("latitude", "longitude", start_index=start_index, end_index=end_index)
            if lat is not None and lon is not None
        ]

        if not points:
            return None

        latitudes = [lat for lat, _ in points]
        longitudes = [lon for _, lon in points]

        min_lat, max_lat = min(latitudes), max(latitudes)
        min_lon, max_lon = min(longitudes), max(longitudes)

        return [[min_lon, min_lat], [max_lon, max_lat]]

    def calc_distance(self, start_index=None, end_index=None):
        # float, distance in km
        distance = 0

        if all([x in self.values_streams for x in ["latitude", "longitude"]]):
            for (prev_lat, prev_lon), (lat, lon) in self._iter_active_adjacent_points(
                "latitude",
                "longitude",
                start_index=start_index,
                end_index=end_index,
            ):
                if prev_lat is not None and prev_lon is not None and lat is not None and lon is not None:
                    distance += haversine((lat, lon), (prev_lat, prev_lon))

        if distance == 0 and "distance" in self.values_streams and len(self.values_streams["distance"]) > 0:
            # use the pre-calculated distance stream if we didn't calculate one
            active_ranges = self._active_index_ranges(start_index=start_index, end_index=end_index)
            if not active_ranges:
                return 0

            distance_m = 0
            distance_values = self.values_streams["distance"]
            for range_start, range_end in active_ranges:
                segment_start = distance_values[range_start]
                segment_end = distance_values[range_end - 1]
                if segment_start is not None and segment_end is not None:
                    distance_m += max(0, segment_end - segment_start)

            return distance_m / 1000

        return distance

    def calc_distance_values(self, start_index=None, end_index=None):
        # list[float], cumulative distance in km
        values = [0]
        if all([x in self.values_streams for x in ["latitude", "longitude"]]):
            for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
                positions = list(
                    zip(self.values_streams["latitude"][range_start:range_end], self.values_streams["longitude"][range_start:range_end])
                )
                if not positions:
                    continue

                if len(values) > 1 or values[0] != 0:
                    values.append(values[-1])

                for prev, pos in zip(positions, positions[1:]):
                    if None in prev or None in pos:
                        values.append(values[-1])
                    else:
                        values.append(values[-1] + haversine(prev, pos))

        return values

    def calc_elapsed_time(self, start_index=None, end_index=None):
        # float, elapsed time in seconds
        if "time" in self.values_streams and self.values_streams["time"]:
            if start_index is not None and end_index is not None:
                return (self.values_streams["time"][end_index] - self.values_streams["time"][start_index]).total_seconds()

            return (self.values_streams["time"][-1] - self.values_streams["time"][0]).total_seconds()

        return 0

    def calc_elevation_gain(self, start_index=None, end_index=None):
        # float, elevation gain in m
        elevation_gain = 0
        if "elevation" in self.values_streams:
            for (previous_point,), (point,) in self._iter_active_adjacent_points(
                "elevation",
                start_index=start_index,
                end_index=end_index,
            ):
                if point is not None and previous_point is not None and point > previous_point:
                    elevation_gain += point - previous_point

        return elevation_gain

    def calc_elevation_loss(self, start_index=None, end_index=None):
        # float, elevation loss in m (positive value)
        elevation_loss = 0
        if "elevation" in self.values_streams:
            for (previous_point,), (point,) in self._iter_active_adjacent_points(
                "elevation",
                start_index=start_index,
                end_index=end_index,
            ):
                if point is not None and previous_point is not None and point < previous_point:
                    elevation_loss -= point - previous_point

        return elevation_loss

    def calc_moving_time(self, *, threshold_m=0.79, start_index=None, end_index=None):
        # float, moving time in seconds
        if not all([x in self.values_streams for x in ["longitude", "latitude", "time"]]):
            return self.calc_elapsed_time(start_index=start_index, end_index=end_index) - self.calc_pause_time(
                start_index=start_index,
                end_index=end_index,
            )

        stationary_seconds = 0
        for (prev_lat, prev_lon, prev_time), (lat, lon, point_time) in self._iter_active_adjacent_points(
            "latitude",
            "longitude",
            "time",
            start_index=start_index,
            end_index=end_index,
        ):
            if prev_lat is not None and prev_lon is not None and lat is not None and lon is not None:
                dist_m = haversine((prev_lat, prev_lon), (lat, lon)) * 1000
                if dist_m < threshold_m:
                    stationary_seconds += (point_time - prev_time).total_seconds()

        active_seconds = self.calc_elapsed_time(start_index=start_index, end_index=end_index) - self.calc_pause_time(
            start_index=start_index,
            end_index=end_index,
        )

        return active_seconds - stationary_seconds

    def calc_time_in_zone(self, zones: list[int], stream: str, start_index=None, end_index=None):
        # list[float], time per zone in seconds
        zone_time_seconds = [0 for _ in range(len(zones))]

        # special case for this calculated stream
        if stream == "pace":
            time_ranges = self._active_stream_by_range("time", start_index=start_index, end_index=end_index)
            values_ranges = [
                self.calc_windowed_pace(window=15, start_index=range_start, end_index=range_end)
                for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index)
            ]
        elif stream == "gap":
            time_ranges = self._active_stream_by_range("time", start_index=start_index, end_index=end_index)
            values_ranges = [
                self.calc_windowed_grade_adjusted_pace(window=15, start_index=range_start, end_index=range_end)
                for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index)
            ]
        elif stream in ("heart_rate", "power"):
            if stream not in self.values_streams:
                return zone_time_seconds

            windowed_fn = self.calc_windowed_heart_rate if stream == "heart_rate" else self.calc_windowed_power
            time_ranges = self._active_stream_by_range("time", start_index=start_index, end_index=end_index)
            values_ranges = [
                windowed_fn(window=15, start_index=range_start, end_index=range_end)
                for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index)
            ]
        else:
            if stream not in self.values_streams:
                return zone_time_seconds

            time_ranges = self._active_stream_by_range("time", start_index=start_index, end_index=end_index)
            values_ranges = self._active_stream_by_range(stream, start_index=start_index, end_index=end_index)

        for times, values in zip(time_ranges, values_ranges):
            prev_dt = None
            prev_val = None

            for dt, val in zip(times, values):
                elapsed = (dt - prev_dt).total_seconds() if prev_dt else 0

                if val is None and prev_val is None:
                    prev_dt = dt
                    continue

                if val is None:
                    val = prev_val

                for i, z in enumerate(zones):
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
        # list[float], seconds per split
        if not all([x in self.values_streams for x in ["longitude", "latitude", "time"]]):
            return []

        splits = []
        next_split = split
        split_start = None
        distance = 0
        segment_elapsed = 0

        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            points = list(
                zip(
                    self.values_streams["latitude"][range_start:range_end],
                    self.values_streams["longitude"][range_start:range_end],
                    self.values_streams["time"][range_start:range_end],
                )
            )

            prev = None
            for lat, lon, clock in points:
                if split_start is None:
                    split_start = segment_elapsed

                if lat is None or lon is None:
                    continue

                if prev:
                    segment_elapsed += (clock - prev[2]).total_seconds()
                    distance += haversine((prev[0], prev[1]), (lat, lon))
                    if distance >= next_split:
                        splits.append(segment_elapsed - split_start)
                        split_start = segment_elapsed
                        next_split += split

                prev = (lat, lon, clock)

        return splits

    def calc_pace(self, start_index=None, end_index=None):
        # float, pace in seconds per km
        t = self.calc_elapsed_time(start_index=start_index, end_index=end_index)
        d = self.calc_distance(start_index=start_index, end_index=end_index)

        if not t or not d:
            return 0

        return t / d

    def calc_moving_pace(self, start_index=None, end_index=None):
        # float, moving pace in seconds per km
        t = self.calc_moving_time(start_index=start_index, end_index=end_index)
        d = self.calc_distance(start_index=start_index, end_index=end_index)

        if not t or not d:
            return 0

        return t / d

    def calc_grade_adjusted_pace(
        self,
        start_index=None,
        end_index=None,
        *,
        grade_smoothing_seconds=GRADE_ADJUSTED_PACE_GRADE_SMOOTHING_SECONDS,
    ):
        # float, grade-adjusted pace in seconds per km
        equivalent_flat_distance = 0
        elapsed_seconds = 0

        if not all([x in self.values_streams for x in ["longitude", "latitude", "time", "elevation"]]):
            return self.calc_moving_pace(start_index=start_index, end_index=end_index)

        for gap in self._iter_grade_adjusted_gap_data(
            start_index=start_index,
            end_index=end_index,
            grade_smoothing_seconds=grade_smoothing_seconds,
        ):
            if gap is None:
                continue
            cost = self._running_energy_cost_for_grade(gap["smoothed_grade_ratio"])
            equivalent_flat_distance += gap["distance_km"] * (cost / GRADE_ADJUSTED_PACE_BASE_COST)
            elapsed_seconds += gap["elapsed_seconds"]

        if not equivalent_flat_distance or not elapsed_seconds:
            return 0

        return elapsed_seconds / equivalent_flat_distance

    def calc_pace_values(self, start_index=None, end_index=None):
        # list[float], pace per point in seconds per km
        if not all([x in self.values_streams for x in ["longitude", "latitude", "time"]]):
            return []

        pace_values = []
        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            prev = None
            points = zip(
                self.values_streams["latitude"][range_start:range_end],
                self.values_streams["longitude"][range_start:range_end],
                self.values_streams["time"][range_start:range_end],
            )

            for lat, lon, clock in points:
                if lat is None or lon is None:
                    pace_values.append(None)
                    continue

                if prev:
                    t = (clock - prev[2]).total_seconds()
                    d = haversine((prev[0], prev[1]), (lat, lon))
                    pace_values.append(None if not t or not d else t / d)
                else:
                    pace_values.append(None)

                prev = (lat, lon, clock)

        return pace_values

    def calc_grade_adjusted_pace_values(
        self,
        start_index=None,
        end_index=None,
        *,
        grade_smoothing_seconds=GRADE_ADJUSTED_PACE_GRADE_SMOOTHING_SECONDS,
    ):
        # list[float], grade-adjusted pace per point in seconds per km
        if not all([x in self.values_streams for x in ["longitude", "latitude", "time", "elevation"]]):
            return []

        gap_values = []
        for gap in self._iter_grade_adjusted_gap_data(
            start_index=start_index,
            end_index=end_index,
            grade_smoothing_seconds=grade_smoothing_seconds,
        ):
            if gap is None:
                gap_values.append(None)
                continue
            cost = self._running_energy_cost_for_grade(gap["smoothed_grade_ratio"])
            gap_values.append((gap["elapsed_seconds"] / gap["distance_km"]) * (GRADE_ADJUSTED_PACE_BASE_COST / cost))

        return gap_values

    def calc_grade_values(self, start_index=None, end_index=None):
        # list[float], grade per point as a percentage
        if not all([x in self.values_streams for x in ["longitude", "latitude", "elevation"]]):
            return []

        grade_values = []
        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            prev = None
            points = zip(
                self.values_streams["latitude"][range_start:range_end],
                self.values_streams["longitude"][range_start:range_end],
                self.values_streams["elevation"][range_start:range_end],
            )

            for lat, lon, elev in points:
                if lat is None or lon is None or elev is None:
                    grade_values.append(None)
                    continue

                if prev:
                    horizontal_distance_m = haversine((prev[0], prev[1]), (lat, lon)) * 1000
                    elevation_change_m = elev - prev[2]
                    grade_values.append((elevation_change_m / horizontal_distance_m) * 100 if horizontal_distance_m > 0 else 0)
                else:
                    grade_values.append(None)

                prev = (lat, lon, elev)

        return grade_values

    def calc_fastest_x(self, distance, start_index=None, end_index=None):
        # int, seconds for fastest segment of the given distance in km
        if not all([x in self.values_streams for x in ["latitude", "longitude", "time"]]):
            return 0

        if distance is None or distance <= 0:
            return 0

        distance_values = self.calc_distance_values(start_index=start_index, end_index=end_index)
        time_values = self._active_stream("time", start_index=start_index, end_index=end_index)

        if not distance_values or not time_values:
            return 0

        fastest = None
        for idx, (start_distance, start_time) in enumerate(zip(distance_values, time_values)):
            if start_time is None:
                continue

            target_distance = start_distance + distance
            end_idx = bisect_left(distance_values, target_distance, lo=idx + 1)
            if end_idx >= len(distance_values):
                continue

            end_time = time_values[end_idx]
            if end_time is None:
                continue

            elapsed_seconds = (end_time - start_time).total_seconds()
            if fastest is None or elapsed_seconds < fastest:
                fastest = elapsed_seconds

        return fastest if fastest is not None else 0

    def calc_windowed_pace(self, window=5, start_index=None, end_index=None):
        # list[float], windowed pace in seconds per km
        result = []
        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            time_values = self.values_streams["time"][range_start:range_end]
            pace_values = self.calc_pace_values(start_index=range_start, end_index=range_end)
            if time_values:
                result.extend(self._calc_stream_windowed_average(time_values, pace_values, window))

        return result

    def calc_windowed_grade_values(self, window=10, start_index=None, end_index=None):
        # list[float], smoothed grade per point as a percentage
        result = []
        for smoothed_grades in self._smoothed_grade_values_by_range(
            start_index=start_index,
            end_index=end_index,
            window=window,
        ):
            result.extend(smoothed_grades)

        return result

    def calc_windowed_grade_adjusted_pace(
        self,
        window=5,
        start_index=None,
        end_index=None,
        *,
        grade_smoothing_seconds=GRADE_ADJUSTED_PACE_GRADE_SMOOTHING_SECONDS,
    ):
        # list[float], windowed grade-adjusted pace in seconds per km
        result = []
        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            time_values = self.values_streams["time"][range_start:range_end]
            gap_values = self.calc_grade_adjusted_pace_values(
                start_index=range_start,
                end_index=range_end,
                grade_smoothing_seconds=grade_smoothing_seconds,
            )
            if time_values:
                result.extend(self._calc_stream_windowed_average(time_values, gap_values, window))

        return result

    def calc_average_heart_rate(self, start_index=None, end_index=None):
        # float, average heart rate in bpm
        return self._calc_stream_average("heart_rate", start_index, end_index)

    def calc_max_heart_rate(self, start_index=None, end_index=None):
        values = self.calc_windowed_heart_rate(window=15, start_index=start_index, end_index=end_index)
        values = [v for v in values if v]
        if not values:
            return 0
        return max(values)

    def calc_heart_rate_recovery(self, recovery_seconds=120, start_index=None, end_index=None):
        hr_values = self.calc_windowed_heart_rate(window=15, start_index=start_index, end_index=end_index)
        clock_values = self.calc_clock_values(start_index=start_index, end_index=end_index)
        if not hr_values or not clock_values:
            return 0

        max_drop = 0
        for i, hr in enumerate(hr_values):
            if not hr:
                continue
            end_time = clock_values[i] + recovery_seconds
            j = bisect_left(clock_values, end_time, lo=i)
            if j >= len(clock_values):
                j = len(clock_values) - 1
            if hr_values[j] and clock_values[j] >= clock_values[i]:
                drop = hr - hr_values[j]
                if drop > max_drop:
                    max_drop = drop

        return max_drop

    def calc_average_power(self, start_index=None, end_index=None):
        # float, average power in watts
        return self._calc_stream_average("power", start_index, end_index)

    def calc_windowed_heart_rate(self, window=15, start_index=None, end_index=None):
        # list[float], windowed heart rate in bpm
        result = []
        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            time_values = self.values_streams["time"][range_start:range_end]
            hr_values = self.values_streams["heart_rate"][range_start:range_end]
            if time_values:
                result.extend(self._calc_stream_windowed_average(time_values, hr_values, window))

        return result

    def calc_windowed_power(self, window=30, start_index=None, end_index=None):
        # list[float], windowed power in watts
        result = []
        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            time_values = self.values_streams["time"][range_start:range_end]
            power_values = self.values_streams["power"][range_start:range_end]
            if time_values:
                result.extend(self._calc_stream_windowed_average(time_values, power_values, window))

        return result

    def calc_clock_values(self, start_index=None, end_index=None):
        # list[float], elapsed time from start in seconds
        if "time" not in self.values_streams:
            return []

        elapsed = []
        total_seconds = 0
        first_point = True
        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            points = [x for x in self.values_streams["time"][range_start:range_end] if x]
            if not points:
                continue

            prev = None
            for point in points:
                if first_point:
                    elapsed.append(0)
                    first_point = False
                elif prev is None:
                    elapsed.append(total_seconds)
                else:
                    total_seconds += (point - prev).total_seconds()
                    elapsed.append(total_seconds)
                prev = point

        return elapsed

    def calc_pause_time(self, start_index=None, end_index=None):
        # float, paused time in seconds
        if not self.segments:
            return 0

        start_time = None
        end_time = None
        if "time" in self.values_streams and self.values_streams["time"]:
            if start_index is not None and end_index is not None:
                start_time = self.values_streams["time"][start_index]
                end_time = self.values_streams["time"][end_index]
            else:
                start_time = self.values_streams["time"][0]
                end_time = self.values_streams["time"][-1]

        if start_time is None or end_time is None:
            return 0

        elapsed_seconds = (end_time - start_time).total_seconds()
        active_seconds = 0
        for segment_start, segment_end in zip(self.segments[::2], self.segments[1::2]):
            overlap_start = max(segment_start, start_time)
            overlap_end = min(segment_end, end_time)
            if overlap_end > overlap_start:
                active_seconds += (overlap_end - overlap_start).total_seconds()

        return elapsed_seconds - active_seconds

    def _calc_stream_average(self, stream_name, start_index=None, end_index=None):
        if stream_name not in self.values_streams:
            return 0

        points = [x for x in self._active_stream(stream_name) if x]
        if start_index is not None and end_index is not None:
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

    def _smoothed_grade_values_by_range(self, start_index=None, end_index=None, window=GRADE_ADJUSTED_PACE_GRADE_SMOOTHING_SECONDS):
        smoothed_grades = []
        for range_start, range_end in self._active_index_ranges(start_index=start_index, end_index=end_index):
            time_values = self.values_streams["time"][range_start:range_end]
            grade_values = self.calc_grade_values(start_index=range_start, end_index=range_end)
            smoothed_grades.append(self._calc_stream_windowed_average(time_values, grade_values, window) if time_values else [])

        return smoothed_grades

    @staticmethod
    def _running_energy_cost_for_grade(grade):
        # Minetti-style running cost model, with grade expressed as rise/run.
        # Reference summaries of the polynomial used here:
        # https://taktyk.pl/sport/trail-running/gap-calculation
        # https://hashiri.ai/knowledge/grade-adjusted-pace
        min_grade = -0.45
        max_grade = 0.45
        grade_5_coefficient = 155.4
        grade_4_coefficient = -30.4
        grade_3_coefficient = -43.3
        grade_2_coefficient = 46.3
        grade_1_coefficient = 19.5

        grade = max(min_grade, min(max_grade, grade))
        return (
            grade_5_coefficient * grade**5
            + grade_4_coefficient * grade**4
            + grade_3_coefficient * grade**3
            + grade_2_coefficient * grade**2
            + grade_1_coefficient * grade
            + GRADE_ADJUSTED_PACE_BASE_COST
        )

    def _iter_grade_adjusted_gap_data(
        self,
        *,
        start_index=None,
        end_index=None,
        grade_smoothing_seconds=GRADE_ADJUSTED_PACE_GRADE_SMOOTHING_SECONDS,
    ):
        smoothed_grade_ranges = self._smoothed_grade_values_by_range(
            start_index=start_index,
            end_index=end_index,
            window=grade_smoothing_seconds,
        )

        for (range_start, range_end), smoothed_grades in zip(
            self._active_index_ranges(start_index=start_index, end_index=end_index),
            smoothed_grade_ranges,
        ):
            points = list(
                zip(
                    self.values_streams["latitude"][range_start:range_end],
                    self.values_streams["longitude"][range_start:range_end],
                    self.values_streams["time"][range_start:range_end],
                    self.values_streams["elevation"][range_start:range_end],
                )
            )

            prev = None
            for idx, (lat, lon, clock, elev) in enumerate(points):
                if prev is None:
                    prev = (lat, lon, clock, elev)
                    yield None
                    continue

                prev_lat, prev_lon, prev_time, prev_elev = prev
                prev = (lat, lon, clock, elev)

                if None in (prev_lat, prev_lon, prev_time, prev_elev, lat, lon, clock, elev):
                    yield None
                    continue

                distance_km = haversine((prev_lat, prev_lon), (lat, lon))
                elapsed_seconds = (clock - prev_time).total_seconds()
                if not distance_km or not elapsed_seconds:
                    yield {
                        "distance_km": distance_km,
                        "elapsed_seconds": elapsed_seconds,
                        "raw_grade_ratio": 0,
                        "smoothed_grade_ratio": 0,
                    }
                    continue

                raw_grade_ratio = (elev - prev_elev) / (distance_km * 1000)
                smoothed_grade = smoothed_grades[idx]
                smoothed_grade_ratio = raw_grade_ratio if smoothed_grade is None else smoothed_grade / 100

                yield {
                    "distance_km": distance_km,
                    "elapsed_seconds": elapsed_seconds,
                    "raw_grade_ratio": raw_grade_ratio,
                    "smoothed_grade_ratio": smoothed_grade_ratio,
                }

    @staticmethod
    def _extract_segments_from_fit_events(events, time_values):
        if not time_values:
            return []

        activity_start = time_values[0]
        activity_end = time_values[-1]
        segment_start = None
        segments = []

        for event in events:
            if event.get_value("event", fallback=None) != "timer":
                continue

            timestamp = event.get_value("timestamp", fallback=None)
            event_type = event.get_value("event_type", fallback=None)

            if not timestamp or not event_type:
                continue

            if event_type == "start":
                segment_start = timestamp
            elif event_type in {"stop", "stop_all", "stop_disable_all"} and segment_start and timestamp >= segment_start:
                segments.extend([segment_start, timestamp])
                segment_start = None

        if segment_start is not None and activity_end >= segment_start:
            segments.extend([segment_start, activity_end])

        return segments or [activity_start, activity_end]

    def _serialize_segment_timestamps(self):
        return [segment.isoformat() if segment else None for segment in self.segments]

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
