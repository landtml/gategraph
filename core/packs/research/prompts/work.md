# Work prompt fragment (research pack)

Every factual claim in the report goes on its own line, in this form:

    claim: <name> = <number> | source: sources/<file>

- `<name>` is a snake_case identifier, unique within the report.
- `<number>` is the value as the source states it, with no unit conversion.
- `<file>` is a file you have placed under `sources/`. Quote from it; never
  cite a source you have not read.

Write a claim line only for a number the source states. Prose may say
anything the sources support; it may not carry a number that has no claim line.
