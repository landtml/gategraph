---
title: "Synthetic throughput note"
sections: ["Summary", "Method", "Results", "Limitations"]
pages: [1, 1]
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

## Limitations

The stations are modelled as fixed times with one source of noise. A real line
has breakdowns, shift changes and operators who learn, none of which appear
here. The numbers describe the model, not any real plant.

## Appendix

The table lists each of the twenty seeded runs. The runs are listed in seed order, and each line gives the finished count and the idle share of the bottleneck station. The appendix adds no new claim. It shows how the summary figures were assembled from the individual runs, so a reader can check the mean and the range by hand.

- Run 01: 401 widgets, bottleneck idle 16 percent.
- Run 02: 402 widgets, bottleneck idle 17 percent.
- Run 03: 403 widgets, bottleneck idle 18 percent.
- Run 04: 404 widgets, bottleneck idle 19 percent.
- Run 05: 405 widgets, bottleneck idle 20 percent.
- Run 06: 406 widgets, bottleneck idle 21 percent.
- Run 07: 407 widgets, bottleneck idle 15 percent.
- Run 08: 408 widgets, bottleneck idle 16 percent.
- Run 09: 409 widgets, bottleneck idle 17 percent.
- Run 10: 410 widgets, bottleneck idle 18 percent.
- Run 11: 411 widgets, bottleneck idle 19 percent.
- Run 12: 412 widgets, bottleneck idle 20 percent.
- Run 13: 413 widgets, bottleneck idle 21 percent.
- Run 14: 414 widgets, bottleneck idle 15 percent.
- Run 15: 415 widgets, bottleneck idle 16 percent.
- Run 16: 416 widgets, bottleneck idle 17 percent.
- Run 17: 417 widgets, bottleneck idle 18 percent.
- Run 18: 418 widgets, bottleneck idle 19 percent.
- Run 19: 419 widgets, bottleneck idle 20 percent.
- Run 20: 420 widgets, bottleneck idle 21 percent.
