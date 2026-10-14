# Deck archive bot

Manual workflow (Actions → Update deck archive → Run workflow) that:

1. Converts new MTGO tournaments (MTGODecklistCache) into Forge deck archives, skipping decks with cards not scripted in Forge and tournaments with fewer than 5 valid decks.
2. Pushes the new zips to `bot/deck-archive` in your fork of forge-extras. With `open_pr` it also opens or updates the pull request to Card-Forge/forge-extras; otherwise it prints a review link.
3. Appends the new tournaments to `forge-gui/res/lists/net-decks-archive-*.txt` and pushes them to `bot/deck-archive-lists` in your fork of Forge. With `open_pr` it opens or updates a draft pull request to Card-Forge/forge; otherwise it prints a review link.

The first job processes third-party data and has no secrets. The second job has the `PR_TOKEN` secret, only handles regular files with expected names, never overwrites existing archives and only accepts list changes that add new lines and leave the existing ones untouched.

Requirements: forks of Card-Forge/forge and Card-Forge/forge-extras under the same account as this repository, and a `PR_TOKEN` secret (classic personal access token, scope `public_repo`).
