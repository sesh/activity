from activity import Activity
from activity.utils import format_mins_seconds
import sys


if __name__ == "__main__":
    fn = sys.argv[-1]
    a = Activity.load(fn)

    # print(a.as_json(include_streams=False, indent=2))

    # hr
    time_in_zone = a.calc_time_in_zone([145, 153, 162, 171, 176, 181, 250], "heart_rate")
    print([format_mins_seconds(x) for x in time_in_zone])
    print(a.calc_elapsed_time(), sum(time_in_zone))

    # power
    time_in_zone = a.calc_time_in_zone([165, 225, 270, 315, 360, 450, 1000], "power")
    print([format_mins_seconds(x) for x in time_in_zone])
    print(a.calc_elapsed_time(), sum(time_in_zone))

    # power
    time_in_zone = a.calc_time_in_zone([322, 284, 264, 249, 241, 223, 0], "pace")
    print([format_mins_seconds(x) for x in time_in_zone])
    print(a.calc_elapsed_time(), sum(time_in_zone))