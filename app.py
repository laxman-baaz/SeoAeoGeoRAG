from crawler import scan_page
from agent import run_full_audit


def main():
    print("AI SEO + GEO + AEO Audit (multi-agent)\n")

    url = input("Enter page URL: ").strip()

    print("\nScanning page in depth...")
    result = scan_page(url)
    if "error" in result:
        print("Error:", result["error"])
        return
    print("Scan complete. Running 3 specialist agents (this takes a few minutes)...")

    out = run_full_audit(result["domain"], result["url"])

    for dim in ("SEO", "AEO", "GEO"):
        print(f"\n===== {dim} REPORT (score: {out['scores'][dim]}) =====\n")
        print(out["sections"][dim])

    print(f"\n===== COMPOSITE SCORE: {out['composite']}/100 =====\n")
    print(out["summary"])


if __name__ == "__main__":
    main()
