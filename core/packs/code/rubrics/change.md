# Rubric: a change to a repository

A verifier reads the unit (variant.py and its tests) and answers each item
with pass or fail, citing the file and line for every fail.

1. One lever. The change alters one behaviour. Unrelated edits are a fail.
2. Tests describe the change. Each behaviour the change claims has a test that
   fails before the change and passes after it.
3. No weakened tests. An existing assertion was not removed, loosened or skipped
   to make the change pass.
4. No hard-coded answers. The code computes its results; it does not special-case
   the inputs a test uses.
5. No hidden effects. The change does not reach the network, the clock, or the
   environment unless the task names it.
6. Names and docstrings match behaviour. A reader of the function signature and
   docstring can predict what it returns.
