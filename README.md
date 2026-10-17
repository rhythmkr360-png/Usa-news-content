# Military Brief — news publishing service

This is the **public news-content repository** for [USA-NEWS](https://github.com/rhythmkr360-png/USA-NEWS), an independent Flutter Android app for readers interested in U.S. military news. It does not represent the U.S. government, the U.S. military, or any news publisher.

The app reads `feed.json` over public HTTPS. The repository contains **source code and JSON only**, not any APK or AAB.

## What it does

Every three hours, GitHub Actions checks the [official DVIDS news RSS feed](https://www.dvidshub.net/about/faq), independently published defense reporting (Breaking Defense, Defense News, USNI News and Task & Purpose), and topic-based Google News RSS searches. The latter sources are configurable and may change availability. It parses RSS/Atom, checks article dates and HTTPS URLs, filters for military relevance, removes duplicates, assigns a topic, and writes pages of JSON for the Android reader. Only stories from the last 72 hours qualify; previously published IDs are remembered to avoid reposting. If all sources fail, the last published feed stays unchanged.

An **optional OpenAI API step** rewrites eligible source excerpts into short, neutral summaries. It only receives titles and RSS excerpts, not full articles, so the output is restricted to those facts. Without an API key, the original feed excerpts are used. Sources with no informative excerpt are skipped rather than having invented details added. Automated summaries can still contain mistakes; publishers should review output periodically.

The app can process up to 150 newly eligible articles per run and store up to 1,000 current articles, split into pages of 40. This is *capacity*, not a claim that any given day has 1,000 suitable articles. News quantity depends on sources, publisher permission and relevance filters.

## How to start

1. Open **Actions → Refresh U.S. military news → Run workflow** on this repository. Leave scheduled GitHub Actions enabled for updates every three hours (GitHub may delay scheduled runs).
2. Wait for the run to pass tests and commit `feed.json` and `pages/`. If no suitable stories were found, the feed stays empty instead of showing fake news.
3. The Flutter app uses this publicly readable URL:

   `https://raw.githubusercontent.com/rhythmkr360-png/Usa-news-content/main/feed.json`

4. For model-based summaries, create a repository **Actions secret** named `OPENAI_API_KEY` with your **OpenAI API platform key**. A ChatGPT subscription does not itself provide API credits. Keep API keys out of code and news JSON. Without the secret, publication still works using validated RSS excerpts.

## Run locally

Requires Python 3.11+; there are no pip dependencies.

```sh
python -m unittest discover -s tests -v
python scripts/update_feed.py --dry-run
python scripts/update_feed.py
```

`--dry-run` fetches and checks sources without modifying files. For optional AI rewrites, export `OPENAI_API_KEY` first. Configurable environment settings: `MAX_NEW_PER_RUN` (default 150, limit 1000), `MAX_AI_ARTICLES` (100), `MAX_STORED_ARTICLES` (1000), `MAX_AGE_HOURS` (72), `HISTORY_DAYS` (10), `PAGE_SIZE` (40), `OPENAI_MODEL` (`gpt-4.1-mini`). The Actions job uses a secret if present; no local API key is required.

## App feed contract (version 1)

```json
{
  "version": 1,
  "generatedAt": "2026-10-17T12:00:00Z",
  "articles": [
    {
      "id": "stable-24-hex-id",
      "title": "An example headline, not an actual news item",
      "summary": "Example of a short, factual description from the source.",
      "category": "Navy",
      "sourceName": "Publishing organization",
      "sourceUrl": "https://publisher.example/story",
      "publishedAt": "2026-10-17T11:45:00Z"
    }
  ],
  "nextPage": "pages/page-2.json"
}
```

`nextPage` is omitted on the final page. Each later page is under `pages/page-N.json` and refers to `page-(N+1).json` relative to its own folder. Articles always link to their reported publishers or aggregator source URLs. No article body, image, or copyrighted photograph is copied into this repository.

## Operating notes

- **Transparency:** Do not imply that this app is an official military or government product. Credit sources and link to original reporting. News can be biased or wrong even when summarized accurately.
- **Licensing:** Publishers own their work. Do not copy whole articles or photographs; check permissions before adding sources or media. Source excerpts may also have usage restrictions. Google News is used for discovery and may change its feed or link behavior without notice.
- **Quality:** DVIDS is a U.S. military public-information source, not independent editorial reporting. Cross-check military claims with other reporting, especially fast-changing events.
- **Security:** The repository and feed are public. Never place private tokens, logs with credentials, or unreleased operational information in news files. Use repository secrets.
- **Alerts:** Inspect failed Actions runs. Host delays, RSS changes, rate limits, missing summaries, or insufficient sources can reduce volume. An API error falls back to RSS excerpts, not fabricated stories.
- **Limitations:** This service collects and writes summaries, but does not do independent fact-checking. Model-based changes should be periodically reviewed by a human before being treated as verified reporting.

