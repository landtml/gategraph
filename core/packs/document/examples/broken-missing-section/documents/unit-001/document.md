---
title: "Synthetic throughput note"
sections: ["Summary", "Method", "Results", "Limitations"]
pages: [1, 2]
---
# Synthetic throughput note

## Summary

This note reports a synthetic measurement of how many widgets a fictional
assembly line finishes in one shift. The figures are invented for the example.
They exist only to exercise the document checks.

## Method

The line was modelled as three stations in series. Each station takes a fixed
time per widget, with a small random delay drawn from a seeded generator, so
every run gives the same numbers. The model ran for eight simulated hours and
was repeated twenty times with different seeds.

Each run records the count of finished widgets and the time each station spent
waiting for its neighbour. The counts were averaged over the twenty runs, and
the spread is reported as the range between the lowest and highest run.

## Results

Across the twenty runs the line finished between 412 and 438 widgets per shift,
with a mean of 425. The second station was the bottleneck in every run. It was
idle for about one fifth of the shift, which is consistent with its place in
the sequence.

- Mean finished widgets per shift: 425
- Range across runs: 412 to 438
- Share of time the bottleneck station was waiting: about 20 percent

Doubling the buffer between the first and second stations raised the mean by
9 widgets. That change is small compared with the spread between runs, so this
note does not recommend it on these figures alone.
