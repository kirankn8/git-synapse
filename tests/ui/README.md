# UI smoke test

Boots the real `app.js` and `graph.js` in [jsdom](https://github.com/jsdom/jsdom)
against a **running** Git Synapse API, drives every route, and exercises the main
interactions. It fails on any uncaught JS error, any route that renders empty,
and any interaction that does not take effect.

Optional, and the only part of the project that needs Node.

```bash
docker compose up -d          # the API must be up and have data
cd tests/ui
npm install
npm test
```

What it checks:

- the measure catalogue populates the header chips and the full selector
- all 18 routes render non-trivial content (overview, repos, repo tabs, file
  tabs, pair detail, directory coupling, explore, graph, measures, jobs, run)
- clicking a table row navigates
- clicking a column header re-sorts
- switching the global measure re-ranks and re-labels the tables
- the theme toggle flips
- the omnibox returns grouped results

jsdom has no canvas, so the graph's rendering calls are stubbed; the test
confirms the layout code runs cleanly and the view mounts, not that pixels are
correct.
