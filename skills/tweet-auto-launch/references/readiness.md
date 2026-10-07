# Readiness log (not included in the public snapshot)

In the author's deployment this file is a running log of operator receipts for the tweet
launcher: deployed module hashes, service restarts, provider rule activations, credit usage,
and on-chain proofs gathered while the launcher was being armed. Those receipts identify one
specific deployment, so they are not published.

Keep your own log here when you arm the launcher. For each change, record:

- what was deployed (file hashes) and which tests passed, with the exact command;
- the feed source in use and evidence that posts are actually being delivered;
- the configured caps (per launch, per UTC day, launch count) and who approved them;
- per chain: an exact 5% quote, the real net allocation receipt after a launch, and proof
  that protection armed the position;
- holder-fee routing evidence (a real accrual or claim), or a plain statement that the route
  is not available on that chain.

A model, a fixture test or a delivered post is not a launch receipt. Write down what is still
unverified.
