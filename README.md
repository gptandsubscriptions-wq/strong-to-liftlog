# Strong to Liftlog Converter

Convert Strong workout app CSV exports to Liftlog backup format.

## What This Does

The Strong app exports data in CSV format, but Liftlog uses a gzipped protobuf format. This tool bridges the gap, allowing you to import your historical Strong data into Liftlog.

**This converter uses the actual protobuf definitions from the [Liftlog GitHub repository](https://github.com/LiamMorrow/LiftLog)** to ensure compatibility.

## Features

- Converts all workouts from Strong CSV to Liftlog format
- Preserves: workout name, date, notes, exercises, sets, weight, reps
- Uses Liftlog's actual protobuf schema (ExportedDataDaoV2)
- Handles kg units (Strong exports kg, Liftlog stores as KILOGRAMS)
- Skips warmup sets (marked with "W" in Strong)
- Generates UUIDs for workouts and exercises
- Creates gzipped `.liftlogbackup.gz` file ready for import

## Usage

### Quick Convert (from project directory)

```bash
source venv/bin/activate
python3 strong_to_liftlog.py <strong_export.csv> [output.liftlogbackup.gz]
```

### Examples

Convert a Strong export:
```bash
python3 strong_to_liftlog.py strong1848198752438667861.csv my_workouts.liftlogbackup.gz
```

If no output file is specified, defaults to `liftlog_backup.liftlogbackup.gz`.

### Importing into Liftlog

1. **Copy the file** to your Android device:
   - Using USB transfer, or
   - Using a cloud service (Google Drive, Dropbox, etc.)

2. **Open Liftlog app** on your device

3. **Navigate to Settings > Backup/Restore**

4. **Select "Import from backup"** and choose the `.liftlogbackup.gz` file

## Format Notes

### Strong CSV Format
- Delimiter: semicolon (`;`)
- Date format: `YYYY-MM-DD HH:MM:SS`
- Warmup sets marked as `W` in "Set Order" column
- Workout notes in "Workout Notes" column (repeated per row)

### Liftlog Format
- Protobuf messages from official Liftlog repository
- Uses `ExportedDataDaoV2` as root message
- Sessions contain: `id` (UuidDao bytes), `session_name`, `date` (DateOnlyDao), `blueprint_notes`, `recorded_exercises`
- Exercises contain: `exercise_blueprint` (template), `potential_sets` (actual sets with weight/reps)
- Gzipped for storage

## Project Structure

```
strong-to-liftlog/
├── proto/                          # Protobuf definitions from Liftlog repo
│   ├── Utils.proto                 # Common types (UuidDao, DateOnlyDao, DecimalValue, etc.)
│   ├── SessionHistoryDao/          # Session data structures
│   ├── SessionBlueprintDao/        # Exercise template structures
│   ├── ExportedDataDao/            # Root backup message
│   └── ...                         # Other proto files
├── strong_to_liftlog.py            # Main converter script
├── README.md                       # This file
└── venv/                           # Python virtual environment
```

## Dependencies

- Python 3.x
- protobuf (Python package)
- gzip (standard library)
- csv (standard library)

## Development

The protobuf definitions are sourced directly from the official [Liftlog GitHub repository](https://github.com/LiamMorrow/LiftLog). To update them:

```bash
cd /home/saunalserver/projects/LiftLog
git pull
cd ../strong-to-liftlog
rm -rf proto/
cp -r ../LiftLog/proto .
protoc --python_out=. --proto_path=. proto/Utils.proto proto/*.proto proto/*/*.proto
```

## Troubleshooting

**Issue**: "File not found" error
**Fix**: Ensure the Strong CSV path is correct. Use absolute paths if needed.

**Issue**: "ModuleNotFoundError: No module named 'google'"
**Fix**: Install protobuf in the virtual environment: `pip install protobuf`

**Issue**: Import fails in Liftlog with "unexpected format"
**Fix**: This converter now uses the actual protobuf definitions from the Liftlog GitHub repository. If issues persist:
- Ensure you're using the latest version of Liftlog
- Verify the file ends in `.liftlogbackup.gz`
- Check that the Strong CSV export is valid (semicolon-delimited)

## License

MIT License - Free to use and modify.
