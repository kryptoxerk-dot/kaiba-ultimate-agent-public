# TwitterAPI.io contract checked 2026-10-06

- [Authentication](https://docs.twitterapi.io/authentication): `X-API-Key` header; the host is `https://api.twitterapi.io`. Do not confuse it with twitterapis.com or twitterxapi.com.
- [Account balance](https://docs.twitterapi.io/api-reference/endpoint/get_my_info): `GET /oapi/my/info`; observed numeric `recharge_credits` and `total_bonus_credits`. One USD is 100,000 credits. Bonus expiry must be checked separately.
- [Advanced search](https://docs.twitterapi.io/api-reference/endpoint/tweet_advanced_search): `GET /twitter/tweet/advanced_search`, `query`, `queryType=Latest`, optional `cursor`; at most 20 posts per page. `tweets`, `has_next_page`, `next_cursor` are top-level. `since_time:` and `until_time:` accept epoch seconds; the docs reject the older datetime-with-UTC syntax.
- [Pricing](https://twitterapi.io/pricing): $0.15 per 1,000 returned tweets, minimum 15 credits per call. At that rate $20 is approximately 133,333 posts before other charges. This is capacity, not a target. A continuous sweep of a full J7 roster (~1,500 handles) every five minutes would consume the budget rapidly.

Use J7 for the streaming account coverage and this API for targeted history/contract searches. Cache and deduplicate by post ID. Start with at most 100 one-page queries per day (up to about $0.30 reserved); the helper's default daily ceiling remains $0.50. These spending defaults are data budgets, not trading filters.

The helper uses an atomic lock directory and atomic replacement of a JSON ledger. An interrupted process may leave a lock; inspect its holder before removing it, and preserve the ledger. Do not run shared jobs with different ledger files. Account calls reserve one minimum unit; tweet calls reserve 20 units. Reserved credits stay charged to our allowance even if the request fails. This bounds risk conservatively but may stop earlier than vendor balance exhaustion.

Output only normalized public post fields and numeric balance fields. Never save raw HTTP responses, headers or exception messages that might echo a secret. No provider write endpoints, monitoring subscriptions, Twitter login cookies or social posting are required.
