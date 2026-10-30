\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{ldconreport}[2026/10/25 LDCon scientific report 1.0]
\LoadClass[11pt,a4paper]{article}
\RequirePackage{iftex}
\ifLuaTeX\else\ClassError{ldconreport}{Use LuaLaTeX}{Run lualatex, not pdflatex.}\fi
\RequirePackage{fontspec}
\setmainfont{Latin Modern Roman}
\setsansfont{Latin Modern Sans}
\setmonofont[Scale=MatchLowercase]{Latin Modern Mono}
\RequirePackage[margin=23mm,headheight=15pt]{geometry}
\RequirePackage{microtype}
\RequirePackage{graphicx}
\RequirePackage{booktabs,tabularx,longtable,array}
\RequirePackage{amsmath,amssymb}
\RequirePackage{xcolor}
\RequirePackage{caption}
\RequirePackage{fancyhdr}
\RequirePackage{hyperref}
\definecolor{reportink}{HTML}{153B46}
\definecolor{reportaccent}{HTML}{007C83}
\hypersetup{colorlinks=true,linkcolor=reportaccent,urlcolor=reportaccent,citecolor=reportaccent}
\captionsetup{font=small,labelfont=bf,justification=raggedright,singlelinecheck=false}
\setlength{\parindent}{0pt}
\setlength{\parskip}{6pt}
\setlength{\emergencystretch}{2em}
\newcommand{\reportshorttitle}{LDCon report}
\pagestyle{fancy}
\fancyhf{}
\fancyhead[L]{\sffamily\small\color{reportink}\reportshorttitle}
\fancyhead[R]{\sffamily\small LDCon}
\fancyfoot[L]{\sffamily\scriptsize Evidence, interpretation and proposal are distinct.}
\fancyfoot[R]{\sffamily\small\thepage}
\renewcommand{\headrulewidth}{0.3pt}
\newcommand{\ReportNotice}[1]{\begin{quote}\small\color{reportink}\textbf{Status.} #1\end{quote}}
% Embed original PDF graphics directly. No rasterization or shell conversion.
% Optional includegraphics arguments may select a page, trim, or crop box.
\newcommand{\ReportFigure}[4][width=\linewidth]{%
  \begin{figure}[htbp]\centering
  \includegraphics[#1]{#2}\caption{#3}\label{#4}
  \end{figure}}
\newcommand{\EvidenceFile}[1]{\texttt{\detokenize{#1}}}
\endinput
