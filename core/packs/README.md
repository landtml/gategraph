# Domain packs

A pack is a directory that declares the parts of one kind of work that core
must not hard-code: artifact readers, checks, tool grants and gate presets.
The layout and the reference packs are in plan section 1.13.

```
packs/<name>/
  pack.yaml      declares everything below (schema: core/kernel/schema/pack.schema.yaml)
  readers/       artifact readers
  checks/        exit-code-contract checks
  rubrics/       verifier rubrics
  prompts/       work and repair prompt fragments
  oracle.py      optional metric computation for the search graph
```

An adapter names the packs it uses in its `packs:` list. The adapter loader
(`core/kernel/config.py`) validates each `pack.yaml` and maps its checks into
the configuration. `core/kernel/packs.py` then loads the rest:

- `load_packs(cfg, known_tools=None, register_reader=None)` returns one `Pack`
  per name in `cfg.packs`, in that order. It raises `PackError` with one
  `path: message` entry per problem.
- A pack check id must be unique in the configuration. An id that an adapter
  check also defines is an error that names both.
- Every tool a pack names must be known to the toolbox. Without a `known_tools`
  argument the built-in names of plan section 1.10 are used.
- Each reader is passed to `register_reader` in pack order, after all checks
  have passed.
- `merge_settings(pack, adapter)` lets adapter settings override pack settings
  key by key. No schema key carries settings yet.

Packs are template-owned: a project changes its adapter, not a pack. A check
that is general enough moves from an adapter into a pack.
