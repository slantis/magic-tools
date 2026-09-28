"""Save GitHub download and traffic numbers for this repository as CSV.

Writes these files into the output folder (the traffic-data branch):

Per day (GitHub keeps only the last 14 days, so each run rewrites the days
it returns and keeps the older ones):

- clones.csv: git clones (count and unique cloners).
- views.csv: page views of the repository (count and unique visitors).

Daily snapshots (one set of rows per run date; a rerun on the same day
replaces that day's rows, so the evolution can be plotted over time):

- release_downloads.csv: cumulative download_count of each release asset.
- referrers.csv: top 10 sites that sent visitors, over the last 14 days.
- paths.csv: top 10 pages of the repository viewed, over the last 14 days.
- repo_stats.csv: stars, forks, watchers and open issues.

Everything is aggregate: GitHub reports no identities for any of these.

Environment:
  GITHUB_REPOSITORY  owner/repo (set by GitHub Actions)
  TRAFFIC_TOKEN      token with read access to repository administration
                     (traffic endpoints reject the default GITHUB_TOKEN)
  GITHUB_TOKEN       default Actions token, used for the other endpoints
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


def read_rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def write_rows(path, fields, rows):
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def merge_daily(path, rows):
    """Upsert rows keyed by date; keep dates GitHub no longer returns."""
    by_date = {r["date"]: r for r in read_rows(path)}
    for row in rows:
        by_date[row["date"]] = row
    write_rows(path, ["date", "count", "uniques"],
               [by_date[d] for d in sorted(by_date)])


def snapshot(path, fields, rows, today):
    """Append today's rows, replacing any earlier run of the same day."""
    kept = [r for r in read_rows(path) if r["snapshot_date"] != today]
    for row in rows:
        row["snapshot_date"] = today
    write_rows(path, ["snapshot_date"] + fields, kept + rows)


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
    out = lambda name: os.path.join(out_dir, name)

    clones = get("/repos/%s/traffic/clones?per=day" % repo, traffic_token)
    merge_daily(out("clones.csv"), traffic_rows(clones, "clones"))

    views = get("/repos/%s/traffic/views?per=day" % repo, traffic_token)
    merge_daily(out("views.csv"), traffic_rows(views, "views"))

    referrers = get("/repos/%s/traffic/popular/referrers" % repo,
                    traffic_token)
    snapshot(out("referrers.csv"), ["referrer", "count", "uniques"],
             [{"referrer": r["referrer"], "count": r["count"],
               "uniques": r["uniques"]} for r in referrers], today)

    paths = get("/repos/%s/traffic/popular/paths" % repo, traffic_token)
    snapshot(out("paths.csv"), ["path", "title", "count", "uniques"],
             [{"path": p["path"], "title": p["title"], "count": p["count"],
               "uniques": p["uniques"]} for p in paths], today)

    releases = get("/repos/%s/releases?per_page=100" % repo, token)
    assets = [{"tag": rel["tag_name"],
               "asset": asset["name"],
               "download_count": asset["download_count"]}
              for rel in releases for asset in rel.get("assets", [])]
    snapshot(out("release_downloads.csv"),
             ["tag", "asset", "download_count"], assets, today)

    info = get("/repos/%s" % repo, token)
    snapshot(out("repo_stats.csv"),
             ["stars", "forks", "watchers", "open_issues"],
             [{"stars": info["stargazers_count"],
               "forks": info["forks_count"],
               "watchers": info.get("subscribers_count", ""),
               "open_issues": info["open_issues_count"]}], today)

    print("clones (14 days): %s total, %s unique" %
          (clones.get("count"), clones.get("uniques")))
    print("views (14 days): %s total, %s unique" %
          (views.get("count"), views.get("uniques")))
    print("referrers: %d, paths: %d, release assets: %d" %
          (len(referrers), len(paths), len(assets)))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".")
