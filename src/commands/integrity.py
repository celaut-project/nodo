from typing import Optional

from src.manager.integrity import check_integrity


def integrity_command(service_ref: Optional[str] = None, fix: bool = False):
    report = check_integrity(service=service_ref, fix=fix)

    print(
        f"Integrity check using {report['hash_name']} ({report['hash_id']}). "
        f"Checked services: {report['checked']}. Fixed: {report['fixed']}",
        flush=True,
    )

    if report["issues"]:
        print("Integrity issues found:", flush=True)
        for issue in report["issues"]:
            fixed_suffix = " [fixed]" if issue["fixed"] else ""
            print(
                f"- {issue['service']} | {issue['code']}: {issue['detail']}{fixed_suffix}",
                flush=True,
            )
    else:
        print("Integrity check passed with no issues.", flush=True)

    return report
