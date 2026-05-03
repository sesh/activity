from activity import Activity
from activity.utils import format_mins_seconds_lstrip
from datetime import datetime
import inspect
import sys
import time


def _list_as_mmss(l):
    return [format_mins_seconds_lstrip(x) for x in l]

def _list_as_percentage(l):
    total = sum(l)
    if not total:
        return ["0.0" for _ in l]
    return [f"{(x / total) * 100:.1f}" for x in l]


def _format_kitchen_sink_value(value):
    if isinstance(value, datetime):
        return value.isoformat()

    if isinstance(value, dict):
        parts = []
        for key in sorted(value.keys()):
            item = value[key]
            if isinstance(item, (list, tuple)):
                parts.append(f"{key}(len={len(item)})={_format_kitchen_sink_value(item)}")
            else:
                parts.append(f"{key}={_format_kitchen_sink_value(item)}")
        return "{" + ", ".join(parts) + "}"

    if isinstance(value, (list, tuple)):
        if len(value) > 10:
            return len(value)
        return "[" + ", ".join(_format_kitchen_sink_value(item) for item in value) + "]"

    return str(value)


def _required_parameters(method):
    signature = inspect.signature(method)
    return [
        parameter
        for parameter in signature.parameters.values()
        if parameter.default is inspect._empty
        and parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]


def activity_one_liner(a, fn):
    parts = [
        fn,
        f"type={a.activity_type or 'unknown'}",
        f"distance={a.distance:.2f}km",
        f"elapsed={format_mins_seconds_lstrip(a.calc_elapsed_time())}",
        f"elapsed_pace={format_mins_seconds_lstrip(a.calc_pace())}",
        f"moving={format_mins_seconds_lstrip(a.calc_moving_time())}",
        f"moving_pace={format_mins_seconds_lstrip(a.calc_moving_pace())}",
    ]

    gap = a.calc_grade_adjusted_pace()
    if gap:
        parts.append(f"gap={format_mins_seconds_lstrip(gap)}/km")

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
    return f"[{elapsed_ms:.1f}ms] " + " | ".join(parts)


def activity_kitchen_sink(a: Activity, fn):
    parts = []
    parts.append(f"fn={fn}")
    for key in sorted(name for name in dir(a) if not name.startswith("_")):
        value = getattr(a, key)
        if callable(value):
            continue

        if key == "values_streams":
            for stream_name in sorted(value.keys()):
                stream_value = value[stream_name]
                parts.append(
                    f"values_streams.{stream_name}(len={len(stream_value)})={_format_kitchen_sink_value(stream_value)}"
                )
            continue

        if isinstance(value, (list, tuple)):
            parts.append(f"{key}(len={len(value)})={_format_kitchen_sink_value(value)}")
        else:
            parts.append(f"{key}={_format_kitchen_sink_value(value)}")

    time_in_zone_configs = {
        "hr": ([145, 153, 162, 171, 176, 181, 999], "heart_rate"),
        "power": ([165, 225, 270, 315, 360, 450, 10000], "power"),
        "pace": ([500, 285, 265, 250, 242, 224, 0], "pace"),
    }
    for label, (zones, stream_name) in time_in_zone_configs.items():
        values = a.calc_time_in_zone(zones, stream_name)
        parts.append(f"time_in_zone_{label}(len={len(values)})={_format_kitchen_sink_value(_list_as_mmss(values))}")
        parts.append(
            f"time_in_zone_{label}_percentage(len={len(values)})={_format_kitchen_sink_value(_list_as_percentage(values))}"
        )

    for method_name in sorted(name for name in dir(a) if not name.startswith("_")):
        if method_name.startswith('as_'):
            continue

        method = getattr(a, method_name)
        if not callable(method):
            continue

        if _required_parameters(method):
            continue

        try:
            value = method()
        except Exception as exc:
            parts.append(f"{method_name}=<ERROR {exc}>")
            continue

        if isinstance(value, (list, tuple)):
            parts.append(f"{method_name}(len={len(value)})={_format_kitchen_sink_value(value)}")
        else:
            parts.append(f"{method_name}={_format_kitchen_sink_value(value)}")

    return "\n".join(parts)


if __name__ == "__main__":
    one_line = False
    for fn in sys.argv[1:]:
        started_at = time.perf_counter()
        a = Activity.load(fn)

        if not a:
            print(f"[ERROR] {fn}")
            continue
        
        if one_line:
            print(activity_one_liner(a, fn))
        else:
            print(activity_kitchen_sink(a, fn))    
