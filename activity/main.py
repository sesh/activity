from activity import Activity
from activity.utils import format_mins_seconds_lstrip
import sys
import time


if __name__ == "__main__":
    for fn in sys.argv[1:]:
        started_at = time.perf_counter()
        a = Activity.load(fn)

        if not a:
            print(f"[ERROR] {fn}")
            continue

        parts = [
            fn,
            f"type={a.activity_type or 'unknown'}",
            f"distance={a.distance:.2f}km",
            f"elapsed={format_mins_seconds_lstrip(a.calc_elapsed_time())}",
            f"moving={format_mins_seconds_lstrip(a.calc_moving_time())}",
        ]

        pause_time = a.calc_pause_time()
        if pause_time:
            parts.append(f"paused={format_mins_seconds_lstrip(pause_time)}")

        if "heart_rate" in a.values_streams:
            parts.append(f"avg_hr={a.calc_average_heart_rate():.0f}bpm")

        if "power" in a.values_streams:
            parts.append(f"avg_power={a.calc_average_power():.0f}W")

        if "cadence" in a.values_streams:
            parts.append(f"avg_cadence={a._calc_stream_average('cadence'):.0f}")

        elapsed_ms = (time.perf_counter() - started_at) * 1000
        print(f"[{elapsed_ms:.1f}ms] " + " | ".join(parts))
