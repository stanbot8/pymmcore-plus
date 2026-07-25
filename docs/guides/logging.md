# Logging

By default, pymmcore-plus logs to the console at the `INFO` level and to a
logfile in the pymmcore-plus application data directory at the `DEBUG` level.
The logfile is named `pymmcore_plus.log` and is rotated at 40MB, with a maximum
retention of 20 logfiles.

## Customizing logging

The [`pymmcore_plus.configure_logging`][] function allows you to customize the
log level, logfile name, and logfile rotation settings.

You may also configure logging using the following environment variables:

| Variable       | Default                                                | Description           |
| -------------- | ------------------------------------------------------ | --------------------- |
| PYMM_LOG_LEVEL | INFO                                                   | The log level.        |
| PYMM_LOG_FILE  | `pymmcore_plus.log` in the pymmcore-plus log directory | The logfile location. |
| PYMM_LOG_RICH  | `0` (disabled)                                         | Use [rich](https://rich.readthedocs.io/) for stderr logging (requires `rich`). Set to `1`, `true`, or `yes` to enable. Note: adds some formatting overhead ([#449](https://github.com/pymmcore-plus/pymmcore-plus/issues/449)). |

!!! tip "pymmcore-plus log directory"

    The application data directory is platform-dependent. Here are the
    log folders for each supported platform:

    | OS     |  Path  |
    | ------ | ------ |
    | macOS  | ~/Library/Application Support/pymmcore-plus/logs |
    | Unix   | ~/.local/share/pymmcore-plus/logs |
    | Win    | C:\Users\username\AppData\Local\pymmcore-plus\pymmcore-plus\logs |

    You can also use `mmcore logs --reveal` to open the log directory in your
    file manager.

On macOS and Linux, pymmcore-plus and CMMCore write to the same log file. On
Windows, each CMMCore instance writes to a separate file in the same directory.
This separation permits each logger to rotate its file safely. The CMMCore file name
contains `cmmcore`, the process ID, and the CMMCore instance number. The
`mmcore logs` command reads these files with the pymmcore-plus log.

## Managing logs with the CLI

The `mmcore` CLI provides a `logs` subcommand for managing logs.

{{ CLI_Logs }}

A particularly useful command is `mmcore logs --tail`, which will continually
stream the current log files to the console. This can be started in another
process and left running to monitor an experiment in progress.

To delete all log files, use `mmcore logs --clear`.
