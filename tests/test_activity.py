import json
from unittest import TestCase
from activity import Activity
from activity.utils import format_mins_seconds
from pathlib import Path


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

    def test_export_as_json(self):
        zwift_ride = Activity.load("tests/testfiles/zwift-ride.fit")
        j = zwift_ride.as_json()

        reloaded = Activity.load_json(json.loads(j))
        self.assertEqual(int(reloaded.distance), 6)

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

    def test_activity_type(self):
        outdoor_run = Activity.load("tests/testfiles/cpt.fit")
        self.assertEqual(outdoor_run.activity_type, "running")

        wod_run = Activity.load("tests/testfiles/applewatch-workoutdoors-run.fit")
        self.assertEqual(wod_run.activity_type, "running")

        indoor_ride = Activity.load("tests/testfiles/zwift-ride.fit")
        self.assertEqual(indoor_ride.activity_type, "cycling")

        indoor_walk = Activity.load("tests/testfiles/applewatch-indoor-walk.fit")
        self.assertEqual(indoor_walk.activity_type, "walking")

        elliptical = Activity.load("tests/testfiles/applewatch-elliptical.fit")
        self.assertEqual(elliptical.activity_type, "fitness_equipment")

        hike = Activity.load("tests/testfiles/applewatch-hike.fit")
        self.assertEqual(hike.activity_type, "hiking")

        garmin_run = Activity.load("tests/testfiles/garmin-run.fit")
        self.assertEqual(garmin_run.activity_type, "running")

        garmin_cycling = Activity.load("tests/testfiles/garmin-cycling.fit")
        self.assertEqual(garmin_cycling.activity_type, "cycling")

    def test_virtual_flag(self):
        zwift_ride = Activity.load("tests/testfiles/zwift-ride.fit")
        self.assertTrue(zwift_ride.virtual)

        reloaded = Activity.load_json(json.loads(zwift_ride.as_json()))
        self.assertTrue(reloaded.virtual)

        outdoor_run = Activity.load("tests/testfiles/cpt.fit")
        self.assertFalse(outdoor_run.virtual)

        reloaded = Activity.load_json(json.loads(outdoor_run.as_json()))
        self.assertFalse(reloaded.virtual)

    def test_auuki_file(self):
        auuki_ride = Activity.load("tests/testfiles/auuki.fit")
        self.assertEqual(auuki_ride.calc_elapsed_time(), 2043.0)
        self.assertEqual(auuki_ride.calc_distance(), 17093.1)

    def test_parse_laps(self):
        w = Activity.load("tests/testfiles/applewatch-workout-laps.fit")
        self.assertEqual(len(w.laps), 10)
        self.assertEqual(round(w.laps[1]["total_elapsed_time"], 0), 360)


class BulkFileLoadingTestCase(TestCase):

    def test_just_load_all_files_without_exception(self):
        for fn in Path("tests/testfiles").glob("*"):
            a = Activity.load(str(fn))


class ActivityCalculationsTestCase(TestCase):

    def test_calcs_full_no_pauses(self):
        #               intervals | runalyze
        # distance           9.71 |     9.72
        # elapsed           44:44 |    44:47
        # elevation gain       99 |       20
        # elevation loss        ~ |       22
        # moving time       44:44 |    44:47
        # elapsed pace       4:36 |     4:36
        # moving pace        4:36 |     4:36

        a = Activity.load("tests/testfiles/applewatch-workout-laps.fit")
        self.assertEqual(f"{a.calc_distance():.2f}", "9.67")
        self.assertEqual(format_mins_seconds(a.calc_elapsed_time()), "00:44:44")
        self.assertEqual(f"{a.calc_elevation_gain():.1f}", "50.6")
        self.assertEqual(f"{a.calc_elevation_loss():.1f}", "52.8")
        self.assertEqual(format_mins_seconds(a.calc_moving_time()), "00:44:40")
        self.assertEqual(format_mins_seconds(a.calc_pace()), "00:04:37")
        self.assertEqual(format_mins_seconds(a.calc_moving_pace()), "00:04:37")
        
    def test_calcs_range_no_pauses(self):
        a = Activity.load("tests/testfiles/applewatch-workout-laps.fit")
        start_index = a.index_at_time(12 * 60)
        end_index = a.index_at_time(18 * 60)

        self.assertEqual(start_index, 720)
        self.assertEqual(end_index, 1080)

        self.assertEqual(f"{a.calc_distance(start_index=start_index, end_index=end_index):.2f}", "1.40")
        self.assertEqual(format_mins_seconds(a.calc_elapsed_time(start_index=start_index, end_index=end_index)), "00:06:00")
        self.assertEqual(format_mins_seconds(a.calc_moving_time(start_index=start_index, end_index=end_index)), "00:06:00")
        self.assertEqual(format_mins_seconds(a.calc_pace(start_index=start_index, end_index=end_index)), "00:04:16")
        self.assertEqual(format_mins_seconds(a.calc_moving_pace(start_index=start_index, end_index=end_index)), "00:04:16")
        
        # less useful
        self.assertEqual(f"{a.calc_elevation_gain(start_index=start_index, end_index=end_index):.1f}", "6.4")
        self.assertEqual(f"{a.calc_elevation_loss(start_index=start_index, end_index=end_index):.1f}", "2.4")
        