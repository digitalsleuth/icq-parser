# ICQ Parser
This tool is designed to provide a web-based view of the contents of the ICQ chat application.

It also parses the contents into JSON files which can be ingested into other tool-sets.

## Usage

### Install
```bash
python3 -m pip install icq-parser
```

Then:

```bash
playwright install chromium
```

This will install the chromium headless version for the desired system. It is only required once, before initial run, and thus internet connectivity is required for this one time only.

### Process

```bash
icq-parser process -h
usage: icq-parser process [-h] {ios,desktop} ...

options:
  -h, --help     show this help message and exit

Device Type:
  {ios,desktop}
    ios          process iOS artifacts
    desktop      process desktop artifacts
```

```bash
icq-parser process ios -h
usage: icq-parser process ios [-h] -s <PATH> -d <PATH> [--log] [--opath <PATH>]

options:
  -h, --help            show this help message and exit
  -s <PATH>, --source <PATH>
                        source path for the iOS ICQ artifacts
  -d <PATH>, --dest <PATH>
                        destination path to save the processing output
  --log                 generates a log file
  --opath <PATH>        source path on original evidence object
```

```bash
icq-parser process desktop -h
usage: icq-parser process desktop [-h] -s <PATH> -d <PATH> [--log] [--opath <PATH>]

options:
  -h, --help            show this help message and exit
  -s <PATH>, --source <PATH>
                        source path for the ICQ Desktop artifacts
  -d <PATH>, --dest <PATH>
                        destination path to save the processing output
  --log                 generates a log file
  --opath <PATH>        source path on original evidence object
```

### Load

```bash
icq-parser load -h
usage: icq-parser load [-h] {ios,desktop} ...

options:
  -h, --help     show this help message and exit

Device Type:
  {ios,desktop}
    ios          load iOS artifacts
    desktop      load desktop artifacts
```

```bash
icq-parser load ios -h
usage: icq-parser load ios [-h] [-i <IP>] [-l] -s <PATH> [--debug] [--log]

options:
  -h, --help            show this help message and exit
  -i <IP>, --ip <IP>    IPv4 address to use for the web interface
  -l, --links           Enable clickable links.
  -s <PATH>, --source <PATH>
                        Path to the source JSON files to display in the web interface
  --debug               Rapid relaunch of script for debugging without indexing
  --log                 generates a log file
```

```bash
icq-parser load desktop -h
usage: icq-parser load desktop [-h] [-i <IP>] [-l] -s <PATH> [--debug] [--log]

options:
  -h, --help            show this help message and exit
  -i <IP>, --ip <IP>    IPv4 address to use for the web interface
  -l, --links           Enable clickable links.
  -s <PATH>, --source <PATH>
                        Path to the source JSON files to display in the web interface
  --debug               Rapid relaunch of script for debugging without indexing
  --log                 generates a log file
```

### Save

```bash
icq-parser save -h
usage: icq-parser save [-h] {ios,desktop} ...

options:
  -h, --help     show this help message and exit

Device Type:
  {ios,desktop}
    ios          save data from iOS processing results
    desktop      save data from desktop processing results
```

```bash
icq-parser save ios -h
usage: icq-parser save ios [-h] [-i <IP>] [-l] -s <PATH> -d <PATH> [--log] [-p] [-m] [-w] [-t TIMEOUT] [--debug]

options:
  -h, --help            show this help message and exit
  -i <IP>, --ip <IP>    IPv4 address to use for the web interface
  -l, --links           Enable clickable links.
  -s <PATH>, --source <PATH>
                        Path to the source JSON files to display in the web interface
  -d <PATH>, --dest <PATH>
                        Location to save the downloaded webpage
  --log                 generates a log file
  -p, --print           Saves the PDF-printed webpages.
  -m, --merge           Merge PDFs into a single PDF, requires -p/--print
  -w, --webpage         Saves the ICQ Parser webpage locally
  -t TIMEOUT, --timeout TIMEOUT
                        Provide a timeout value in seconds for the PDF to PNG generation for large pages
  --debug               Rapid relaunch without indexing, for debugging
```

```bash
icq-parser save desktop -h
usage: icq-parser save desktop [-h] [-i <IP>] [-l] -s <PATH> -d <PATH> [--log] [-p] [-m] [-w] [-t TIMEOUT] [--debug]

options:
  -h, --help            show this help message and exit
  -i <IP>, --ip <IP>    IPv4 address to use for the web interface
  -l, --links           Enable clickable links.
  -s <PATH>, --source <PATH>
                        Path to the source JSON files to display in the web interface
  -d <PATH>, --dest <PATH>
                        Location to save the downloaded data
  --log                 generates a log file
  -p, --print           Saves the PDF-printed webpages.
  -m, --merge           Merge PDFs into a single PDF, requires -p/--print
  -w, --webpage         Saves the ICQ Parser webpage locally
  -t TIMEOUT, --timeout TIMEOUT
                        Provide a timeout value in seconds for the PDF to PNG generation for large pages
  --debug               Rapid relaunch without indexing, for debugging
```
