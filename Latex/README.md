# LaTeX-Template für die Dissertation am Institut für Automatisierungstechnik

Verwendete Versionen:
- VS Code v1.50.0
( - TeXstudio v2.12.16)
 - latexmk v4.67
 - biber-windows-x64 v2.14
 - biblatex v3.14
 - koma-script v3.29

## Hinweise zur Benutzung:

Das TeX-Dokument ist weitestgehen automatisiert. Alle notwendigen Einstellungen werden in `./Einstellungen/Eigenschaften.tex` vorgenommen.

Verfügbare Einstellungen:
 - Titel der Arbeit
 - Name des Autor
 - Geburtsort des Autors
 - Erstgutachter
 - Zweitgutachter
 - Tag der mündlicher Prüfung
 - Jahr der Promotion
 - Deutscher Abstrakt
 - Englischer Abstrakt
 - Keywords

Auf Basis der angegebenen Informationen werden die Titelseiten des Dokuments gesetzt.

In `./Einstellungen/Pakete.tex` werden die benötigten Pakete inkludiert. Hier werden auch die paketspezifischen Einstellungen vorgenommen.

In `./Einstellungen/Formatierung.tex` werden alle Einstellungen vorgenommen, die die Formatierung des Dokuments beeinflussen.

Das Dokument in TeX teilt sich in drei Bereiche: `\frontmatter`, `\mainmatter` und `\backmatter`, also Vorspann, Hauptteil und Nachspann. Der Anhang ist Teil des Hauptteils. Die Zuordnung der Seiten zu den jeweiligen Teilen hat Einfluss auf die Art der Seitenzählen (arabisch/römisch und Zählungsbeginn).

### Vorspann

Der Vorspann setzt sich aus Titelseite(n), Zitat, Vorwort sowie den Verzeichnisse Inhaltsverzeichnis, Abbildungsverzeichnis, Tabellenverzeichnis und dem Abkürzungsverzeichnis. Die ersten drei werden automatisch von TeX erstellt.
Das Abkürzungsverzeichnis wird mit dem TeX-Paket `acronym` umgesetzt. Alle Abkürzungen werden in `./Einstellungen/Abkuerzungen.tex` definiert und können dann im Text über ein Kürzel aufgerufen werden. 

### Hauptteil

Im Hauptteil befindet sich der eigentliche Text. Hier ist es empfehlenswert für jedes Kapitel ein eigenen TeX-File anzulegen und über `\input{./Kapitel/"Kapitelname".tex` einzubinden.

Der Anhang wird mit dem Befehl `\appendix` eingeleitet. Die Inhalte können wieder über eigene .tex-Files eingebunden werden.

### Nachspann

Im Nachspann befindet sich nur das Literaturverzeichnis.

## Verwendung des IfA-Zitationsstils in Kombination mit Citavi

Kurzbelege werden durch dieses Template automatisch gemäß dem gewöhnungsbedürftigen IfA-Stil erstellt. D.h. inklusive aller hochgestellter @, * usw.

Damit die Quellen mit dem Kurzbeleg vom Institut fuer Automatisierungstechnik referenziert werden, ist es zunächst notwendig, in Citavi den Zitationsstil und den Kurzbeleg vom Server zu laden. Zusätzlich muss in Citavi unter [Extras - Optionen - Zitation] die "Kurzbeleg-Unterstuetzung" eingeschaltet werden.

WICHTIG: Damit die Kurzbelege in LaTeX genutzt werden können, müssen sie in das Bibtex-Feld "Shorthand" in Citavi exportiert werden. Dies kann unter [Datei - Exportieren - Exportieren...] eingestellt werden. Dort muss lediglich eine Kopie des bestehenden BibTeX-Exports erstellt werden, bei dem für jeden Dokumententyp der Kurzbeleg als Shorthand exportiert wird. Diese Einstellung muss nur einmalig erstellt werden und kann anschließend für alle Exporte genutzt werden.

## Getrennte Literatur-Verzeichnisse (Eigene Veröffentlichungen, Studentische Arbeiten, etc.)

Damit einzelne "Unter-Literaturverzeichnisse" für die unterschiedlichen Quellen-Arten angelegt und die Kurzbelege richtig (um @, *, %) erweitert werden, MÜSSEN die einzelnen BibTex-Exporte aus Citavi korrekt den richtigen BibTex-Dateien zugeordnet werden. Ihr müsst also darauf achten, eigene Quellen in der Datei "EigenePublikationen.bib" abzulegen usw.

## Export-Preset für Citavi

Für den Export von Quellen in Citavi können Presets angelegt werden. Dazu unter [Datei - Exportieren - Exportieren ...] "Alle Titel in diesem Projekt" auswaehlen und auf der nächten Seite den "BibLaTeX" Exportfilter auswählen. Sollte dieser nicht zur Verfügung stehen kann er über die Schaltfläche "Exportfilter hinzufügen" aus den Vorlagen ergänzt werden. Auf der nächsten Seite "Eine Textdatei erstellen" anwählen und "./Literatur/Literatur.bib" auswählen. Es darf dabei **nur** die BibTeX-Option "Großbuchstaben in geschweifte Klammern setzen" aktiviert sein. Praktisch ist die Funktion "Nur referenzierte Titel aus TeX-Publikation exportieren": Wenn diese aktiviert wird kannüber "Durchsuchen" die "./main.tex" ausgewählt werden und Citavi durchsucht diese und exportiert nur im Dokument verwendete Quellen. Auf der nächsten Seite können die Einstellungen als Vorlage gespeichert werden. Der Export kann dann direkt unter [Datei - Exportieren] aufgerufen werden.

## Weitere Hinweise:
 - Die Pakete hyperref und url vertragen sich nicht. GAR NICHT! Hyperref ist zu praeferieren.
 - Im Draft-Modus werden Links nicht gesetzt/angezeigt. Schwarze Kaesten zeigen Ueberbreiten an.

## Known Bugs:
 - "`LaTeX Warning: Command \InputIfFileExists has changed. Check if current package is valid"' - https://texwelt.de/fragen/25524/warnung-command-inputiffileexists-has-changed
 - "`\ifpdftex was redefined(scrbase) at the document preamble."' - https://komascript.de/node/2262
 - "`\ifVTeX was redefined(scrbase) at the document preamble."' - https://komascript.de/node/2262
 - "`pdflatex (file ./Abbildungen/gra_Unilogo.pdf): PDF inclusion: found PDF version <1.7>, but at most version <1.5> allowed<Abbildungen/gra_Unilogo.pdf, id=3, 143.53625pt x 103.38625pt>File: Abbildungen/gra_Unilogo.pdf Graphic file (type pdf)<use Abbildungen/gra_Unilogo.pdf>Package pdftex.def Info: Abbildungen/gra_Unilogo.pdf used"' - Das HSU Logo wurde von TeX von .eps zu . pdf konvertiert. Leider in eine alte .pdf-Version. 