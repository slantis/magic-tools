"""Save GitHub download numbers for this repository as CSV files.

Writes three files into the output folder (the traffic-data branch):

- clones.csv: one row per day, git clones (count and unique cloners).
  GitHub only keeps the last 14 days, so each run rewrites the days it
  returns and keeps the older ones.
- views.csv: same shape, for page views of the repository.
- release_downloads.csv: one row per release asset per run, with the
  cumulative download_count at that moment. Rows are daily snapshots, so
  the evolution can be plotted, not only the current total.

Environment:
  GITHUB_REPOSITORY  owner/repo (set by GitHub Actions)
  TRAFFIC_TOKEN      token with read access to repository administration
                     (traffic endpoints reject the default GITHUB_TOKEN)
  GITHUB_TOKEN       default Actions token, used for the releases endpoint
"""
import csv
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"


def get(path, token):
    req = urllib.request.Request(API + path, headers={
        "Accept": "application/vnd.github+json",
        "Authorization": "Bearer " + token,
        "X-GitHub-Api-Version": "2022-11-28",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as err:
        if err.code in (401, 403, 404) and "/traffic/" in path:
            # GitHub answers 404, not 403, when a token cannot see a
            # private repository, so a bad token looks like a wrong URL.
            sys.exit("Traffic API returned %d for %s. TRAFFIC_TOKEN has no "
                     "access to this repository: it must be a fine-grained "
                     "token owned by the repository's organization, "
                     "approved by it if required, with Administration: "
                     "Read-only on this repository." % (err.code, path))
        raise


def merge_daily(path, rows):
    """Upsert rows keyed by date; keep dates GitHub no longer returns."""
    by_date = {}
    if os.path.exists(path):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                by_date[row["date"]] = row
    for row in rows:
        by_date[row["date"]] = row
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["date", "count", "uniques"])
        writer.writeheader()
        for date in sorted(by_date):
            writer.writerow(by_date[date])


def traffic_rows(payload, key):
    return [{"date": item["timestamp"][:10],
             "count": item["count"],
             "uniques": item["uniques"]}
            for item in payload.get(key, [])]


def main(out_dir):
    repo = os.environ["GITHUB_REPOSITORY"]
    traffic_token = os.environ["TRAFFIC_TOKEN"]
    token = os.environ.get("GITHUB_TOKEN") or traffic_token
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    clones = get("/repos/%s/traffic/clones?per=day" % repo, traffic_token)
    merge_daily(os.path.join(out_dir, "clones.csv"),
                traffic_rows(clones, "clones"))

    views = get("/repos/%s/traffic/views?per=day" % repo, traffic_token)
    merge_daily(os.path.join(out_dir, "views.csv"),
                traffic_rows(views, "views"))

    releases = get("/repos/%s/releases?per_page=100" % repo, token)
    path = os.path.join(out_dir, "release_downloads.csv")
    fields = ["snapshot_date", "tag", "asset", "download_count"]
    rows = []
    if os.path.exists(path):
        with open(path, newline="") as f:
            # one snapshot per day: a rerun replaces today's rows
            rows = [r for r in csv.DictReader(f)
                    if r["snapshot_date"] != today]
    for rel in releases:
        for asset in rel.get("assets", []):
            rows.append({"snapshot_date": today,
                         "tag": rel["tag_name"],
                         "asset": asset["name"],
                         "download_count": asset["download_count"]})
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    print("clones (14 days): %s total, %s unique" %
          (clones.get("count"), clones.get("uniques")))
    print("release assets tracked today: %d" %
          sum(1 for r in rows if r["snapshot_date"] == today))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
