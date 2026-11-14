A check of the document unit failed. The finding names the check, the rule and
the line. Change only the lines the finding names, then stop.

- `document.section_missing`: add the heading, with content under it.
- `document.placeholder`: replace the marked text with the finished sentence or
  remove the line.
- `document.pages_out_of_bounds`: shorten or lengthen the body to the declared
  pages. Do not change the bounds in the front matter.
- `document.render_failed`: close the code fence the finding names, or remove it.

Do not edit `build/`; it is regenerated when the render runs.
