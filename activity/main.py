from activity import Activity
import sys


if __name__ == "__main__":
    fn = sys.argv[-1]
    a = Activity.load(fn)
    print(a.as_json(include_streams=False, indent=2))
