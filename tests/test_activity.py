import json
from unittest import TestCase
from activity import Activity
from activity.utils import format_mins_seconds


class ActivityTests(TestCase):
    def test_load_gpx_track_without_timestamps(self):
        a = Activity.load("tests/testfiles/buffalo-final.gpx")
        self.assertEqual(int(a.distance), 43)
        self.assertListEqual(a.streams(), ["longitude", "latitude", "elevation"])
        self.assertEqual(a.elapsed_time, 0)
        self.assertEqual(int(a.elevation_gain), 1595)
        self.assertEqual(int(a.elevation_loss), 2610)

    def test_load_gpx_with_extensions(self):
        a = Activity.load("tests/testfiles/fartlek.gpx")
        self.assertEqual(int(a.distance), 8)
        self.assertEqual(
            a.streams(),
            ["longitude", "latitude", "elevation", "heart_rate", "cadence", "time"],
        )
        self.assertEqual(a.elapsed_time, 2400)

    def test_pace_calculations(self):
        a = Activity.load("tests/testfiles/clearspot.gpx")
        self.assertEqual(format_mins_seconds(a.calc_pace()), "00:07:33")

    def test_moving_time_calculations_with_stationary_time(self):
        # GPX file with periods of stationary time

        a = Activity.load("tests/testfiles/nailcan-singletrack.gpx")
        self.assertEqual(format_mins_seconds(a.calc_moving_time()), "00:51:21")

    def test_load_fit(self):
        a = Activity.load(
            "tests/testfiles/cpt-loops.fit",
        )
        self.assertEqual(int(a.distance), 20)

    def test_export_as_geojson(self):
        a = Activity.load("tests/testfiles/nailcan-singletrack.gpx")
        geojson = a.as_geojson()

        geojson_json = json.loads(geojson)
        self.assertEqual(geojson_json["geometry"]["type"], "LineString")

    def test_splits(self):
        a = Activity.load(
            "tests/testfiles/cpt-loops.fit",
        )

        self.assertEqual(
            [
                300.0,
                290.0,
                295.0,
                302.0,
                321.0,
                303.0,
                306.0,
                313.0,
                321.0,
                326.0,
                366.0,
                314.0,
                311.0,
                317.0,
                317.0,
                347.0,
                324.0,
                319.0,
                320.0,
                322.0,
            ],
            a.calc_splits(),
        )

    def test_bounding_box(self):
        a = Activity.load(
            "tests/testfiles/cpt-loops.fit",
        )
        self.assertEqual(
            a.bounding_box,
            [
                [146.8911443464458, -36.092038191854954],
                [146.9069256260991, -36.08304709196091],
            ],
        )

    def test_json_export(self):
        a = Activity.load("tests/testfiles/cpt.fit")
