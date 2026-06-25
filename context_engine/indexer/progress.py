from dataclasses import dataclass


@dataclass
class LineProgress:
    total: int
    desc: str
    unit: str = "item"
    done: int = 0
    _last_bucket: int = -1

    def __post_init__(self):
        print(f"{self.desc}: 0/{self.total} {self.unit}")

    def update(self, n: int = 1):
        self.done += n
        if self.total <= 0:
            return
        percent = min(100, int((self.done / self.total) * 100))
        bucket = percent // 10
        if percent == 100 or bucket > self._last_bucket:
            print(f"{self.desc}: {min(self.done, self.total)}/{self.total} ({percent}%)")
            self._last_bucket = bucket

    def close(self):
        if self.total == 0:
            print(f"{self.desc}: done")
        elif self.done < self.total:
            print(f"{self.desc}: {self.total}/{self.total} (100%)")


def make_progress(total: int, desc: str, unit: str = "item"):
    try:
        from tqdm import tqdm

        return tqdm(total=total, desc=desc, unit=unit, dynamic_ncols=True, leave=True)
    except Exception:
        return LineProgress(total=total, desc=desc, unit=unit)
