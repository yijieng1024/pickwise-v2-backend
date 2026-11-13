"""
The go-live check (plan step 11, decisions 3 to 5).

Every laptop the processor inserts is `inactive` with `go_live_held` set. The check
runs at the end of every processing job, over the held laptops only: one that passes
every condition becomes active and loses the marker, one that fails stays inactive
with the failed conditions stored on it. A laptop without the marker is never
touched, so one an admin set inactive by hand stays as it is.
"""
from sqlalchemy import func
from sqlmodel import Session, select

from app.benchmark.model import CPUBenchmark, GPUBenchmark
from app.laptops.laptop_models import Laptop, LaptopStatus
from app.pickscore.benchmark_service import resolve_benchmark, resolve_gpu_benchmark

_REQUIRED = (
    ("cpu", "processor_model"), ("gpu", "gpu_model"), ("ram", "ram_gb"), ("storage", "ssd_gb"),
    ("screen_size", "display_size_inch"), ("weight", "weight_kg"),
)


def failed_conditions(laptop, same_code_others: int, cpu_benchmarks, gpu_benchmarks) -> list[str]:
    """
    The go-live conditions *laptop* fails; empty means it may go live. Pure: it reads
    the laptop's fields and the two benchmark lists, nothing else.

    *same_code_others* is how many other laptops have its model_code, in any status.
    A null family_id is not a condition: it does not block, the caller reports it.
    """
    failed = []
    if not (laptop.price_rm or 0) > 0:
        failed.append("price")
    for name, field in _REQUIRED:
        value = getattr(laptop, field)
        if not (value.strip() if isinstance(value, str) else value):
            failed.append(name)
    if same_code_others:
        failed.append("duplicate_model_code")
    # The scorer's own resolvers: an unresolved mark is a made-up neutral 50 on a
    # public page. Stricter than the withheld rule, which needs both unresolved.
    cpu, gpu = laptop.processor_model or "", laptop.gpu_model or ""
    if resolve_benchmark(cpu, cpu_benchmarks)["score"] is None:
        failed.append("cpu_benchmark")
    if resolve_gpu_benchmark(gpu, cpu, gpu_benchmarks)["score"] is None:
        failed.append("gpu_benchmark")
    return failed


def run_go_live_check(session: Session) -> dict:
    """
    Check every held laptop and write the outcome. Returns what the job result and
    the summary email show: `activated`, `held` (each with its failed conditions) and
    `no_family` (reported, never a reason to hold).

    Only `inactive` rows are read: a held laptop whose status an admin has since
    changed is the admin's.
    """
    held = session.exec(
        select(Laptop).where(Laptop.go_live_held == True)  # noqa: E712
        .where(Laptop.status == LaptopStatus.INACTIVE.value)
        .order_by(Laptop.created_at, Laptop.id)  # type: ignore[arg-type]
    ).all()
    report: dict = {"checked": len(held), "activated": [], "held": [], "no_family": []}
    if not held:
        return report

    # Read here, not from the scoring cache: that one serves a stale copy while it
    # rebuilds, and a PassMark row that arrived this run is what a held laptop waits for.
    cpu_benchmarks = [(r.cpu_name, r.cpu_mark) for r in session.exec(select(CPUBenchmark)).all()]
    gpu_benchmarks = [(r.gpu_name, r.gpu_mark) for r in session.exec(select(GPUBenchmark)).all()]
    # model_code is unique, so only a code differing in letter case can be a second
    # row. The processor's own lookup is the main guard (decision 4's note).
    codes = [l.model_code.lower() for l in held]
    same_code = dict(session.exec(
        select(func.lower(Laptop.model_code), func.count())
        .where(func.lower(Laptop.model_code).in_(codes)).group_by(func.lower(Laptop.model_code))
    ).all())

    for laptop in held:
        entry = {"laptop_id": str(laptop.id), "model_code": laptop.model_code, "product_name": laptop.product_name}
        failed = failed_conditions(laptop, same_code.get(laptop.model_code.lower(), 1) - 1,
                                   cpu_benchmarks, gpu_benchmarks)
        if failed:
            laptop.go_live_failures = failed
            report["held"].append({**entry, "failed": failed})
        else:
            laptop.status = LaptopStatus.ACTIVE.value
            laptop.go_live_held = False
            laptop.go_live_failures = None
            report["activated"].append(entry)
        if laptop.family_id is None:
            report["no_family"].append(entry)
        session.add(laptop)
    session.commit()
    return report
