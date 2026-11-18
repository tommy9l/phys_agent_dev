# Typesetting rules (save_note_latex)

Once save_note has accepted the Markdown note, write the typeset version
with save_note_latex: a complete LaTeX document (documentclass article,
compiled with xelatex; figures are under ../plots/) containing

- the title block: author lines, version/date, and the public-materials
  disclaimer or the INTERNAL header, whichever applies;
- the abstract and a table of contents;
- the same ten sections in the same order as the accepted Markdown note,
  with the same content;
- booktabs tables in table floats with `\caption` BEFORE `\begin{tabular}`
  (table captions above);
- numbered figure floats with descriptive captions, `\caption` AFTER
  `\includegraphics` (figure captions below);
- a References section (thebibliography) collecting every publication
  cited in the text, with `\cite` commands at the in-text mentions.

Every table must fit the text width: `\small` or `\footnotesize`, short
row labels with abbreviations defined in the caption,
`\resizebox{\textwidth}{!}{...}` or tabularx for wide tables, `p{}`
columns for long text.

If compilation fails, or overfull boxes are reported, fix the LaTeX and
call save_note_latex again (once for overfull boxes).
