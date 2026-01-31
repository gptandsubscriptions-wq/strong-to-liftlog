#!/usr/bin/env python3
"""
Strong to Liftlog Converter

Converts Strong app CSV exports to Liftlog backup format (.liftlogbackup.gz)

Based on the actual Liftlog protobuf definitions from:
https://github.com/LiamMorrow/LiftLog

Usage:
    python strong_to_liftlog.py input.csv output.liftlogbackup
"""

import csv
import sys
import uuid
import gzip
from datetime import datetime
from proto.ExportedDataDao.ExportedDataDaoV2_pb2 import ExportedDataDaoV2
from proto.SessionHistoryDao.SessionHistoryDaoV2_pb2 import SessionDaoV2, RecordedExerciseDaoV2, PotentialSetDaoV2, RecordedSetDaoV2
from proto.SessionBlueprintDao.SessionBlueprintDaoV2_pb2 import ExerciseBlueprintDaoV2, ExerciseType
from proto.Utils_pb2 import UuidDao, DateOnlyDao, DecimalValue, TimeOnlyDao
from proto.FeedStateDao_pb2 import FeedStateDaoV1
from google.protobuf.wrappers_pb2 import StringValue


def parse_date(date_str):
    """Parse Strong datetime format to DateOnlyDao"""
    # Strong format: "2024-08-31 00:03:19"
    dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
    date_dao = DateOnlyDao()
    date_dao.year = dt.year
    date_dao.month = dt.month
    date_dao.day = dt.day
    return date_dao, dt.hour, dt.minute, dt.second


def parse_time_with_progress(h, m, s, set_index):
    """
    Create TimeOnlyDao for a set, adding slight time progression for each set.
    This simulates the time progression between sets.
    """
    time_dao = TimeOnlyDao()
    # Add ~30 seconds per set (typical rest time)
    total_seconds = h * 3600 + m * 60 + s + (set_index * 30)
    time_dao.hour = total_seconds // 3600
    time_dao.minute = (total_seconds % 3600) // 60
    time_dao.second = total_seconds % 60
    time_dao.millisecond = 0
    time_dao.microsecond = 0
    return time_dao


def create_uuid_dao():
    """Create UuidDao from random UUID"""
    u = uuid.uuid4()
    uuid_dao = UuidDao()
    uuid_dao.value = u.bytes
    return uuid_dao


def create_decimal_value(kg_value):
    """Convert kg float to DecimalValue (Liftlog's decimal format)"""
    decimal = DecimalValue()
    # DecimalValue stores decimal as units (int64) + nanos (sfixed32)
    # e.g., 10.5 kg = units=10, nanos=500000000
    if kg_value == int(kg_value):
        decimal.units = int(kg_value)
        decimal.nanos = 0
    else:
        decimal.units = int(kg_value)
        # Convert fractional part to nanos
        decimal.nanos = int(round((kg_value - int(kg_value)) * 1_000_000_000))
    return decimal


def convert_strong_to_liftlog(strong_csv_path):
    """Convert Strong CSV to Liftlog protobuf format"""

    # Read and parse Strong CSV
    workouts = {}

    with open(strong_csv_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter=';')

        for row in reader:
            workout_id = row['Workout #']

            # Create workout if not exists
            if workout_id not in workouts:
                date_dao, hour, minute, second = parse_date(row['Date'])

                workouts[workout_id] = {
                    'id': create_uuid_dao(),
                    'title': row['Workout Name'] or 'Workout',
                    'date': date_dao,
                    'hour': hour,
                    'minute': minute,
                    'second': second,
                    'notes': row['Workout Notes'] or '',
                    'exercises': {}
                }

            exercise_name = row['Exercise Name']
            if exercise_name not in workouts[workout_id]['exercises']:
                workouts[workout_id]['exercises'][exercise_name] = {
                    'name': exercise_name,
                    'sets': []
                }

            # Parse set data (skip warmup sets marked with "W")
            set_order = row['Set Order']
            if set_order != 'W':
                # Parse values with safety
                try:
                    weight = float(row['Weight (kg)']) if row['Weight (kg)'] else 0
                except ValueError:
                    weight = 0

                try:
                    reps = int(float(row['Reps'])) if row['Reps'] else 0
                except ValueError:
                    reps = 0

                rpe = int(row['RPE']) if row['RPE'] else 0

                try:
                    distance = float(row['Distance (meters)']) if row['Distance (meters)'] else 0
                except ValueError:
                    distance = 0

                try:
                    seconds = int(row['Seconds']) if row['Seconds'] else 0
                except ValueError:
                    seconds = 0

                workouts[workout_id]['exercises'][exercise_name]['sets'].append({
                    'weight': weight,
                    'reps': reps,
                    'rpe': rpe,
                    'distance': distance,
                    'duration': seconds
                })

    # Build Liftlog backup
    backup = ExportedDataDaoV2()

    for workout_data in workouts.values():
        session = backup.sessions.add()

        # Set basic fields
        session.id.CopyFrom(workout_data['id'])
        session.session_name = workout_data['title']
        session.date.CopyFrom(workout_data['date'])
        session.blueprint_notes = workout_data['notes']

        for ex_data in workout_data['exercises'].values():
            recorded_exercise = session.recorded_exercises.add()

            # Set exercise blueprint (this defines the exercise template)
            blueprint = recorded_exercise.exercise_blueprint
            blueprint.name = ex_data['name']
            blueprint.type = ExerciseType.WEIGHTED

            # Set the number of sets and reps based on the first set
            if ex_data['sets']:
                blueprint.sets = len(ex_data['sets'])
                # Use reps from first set as template
                blueprint.reps_per_set = ex_data['sets'][0]['reps']

            # Add each set as a potential_set
            for i, set_data in enumerate(ex_data['sets']):
                potential_set = recorded_exercise.potential_sets.add()

                # Create the recorded set (contains actual performance data)
                recorded_set = potential_set.recorded_set
                recorded_set.reps_completed = set_data['reps']
                recorded_set.completion_time.CopyFrom(
                    parse_time_with_progress(
                        workout_data['hour'],
                        workout_data['minute'],
                        workout_data['second'],
                        i
                    )
                )
                # Add completion date to match the sample format
                recorded_set.completion_date.CopyFrom(workout_data['date'])

                # Set weight value
                potential_set.weight_value.CopyFrom(create_decimal_value(set_data['weight']))
                potential_set.weight_unit = 1  # KILOGRAMS

    return backup


def main():
    if len(sys.argv) < 2:
        print("Usage: python strong_to_liftlog.py <strong_export.csv> [output.liftlogbackup.gz]")
        print("\nIf no output file is specified, defaults to: liftlog_backup.liftlogbackup.gz")
        sys.exit(1)

    input_csv = sys.argv[1]

    if len(sys.argv) >= 3:
        output_file = sys.argv[2]
    else:
        output_file = "liftlog_backup.liftlogbackup.gz"

    print(f"Converting {input_csv} to Liftlog format...")
    print(f"Output will be saved to: {output_file}")

    # Convert
    backup = convert_strong_to_liftlog(input_csv)

    print(f"Converted {len(backup.sessions)} workout sessions")

    # Write gzipped protobuf
    with gzip.open(output_file, 'wb') as f:
        f.write(backup.SerializeToString())

    print(f"Successfully created {output_file}")
    print("\nTo import in Liftlog:")
    print("1. Copy the .liftlogbackup.gz file to your device")
    print("2. Open Liftlog app")
    print("3. Go to Settings > Backup/Restore")
    print("4. Select 'Import from backup' and choose the file")


if __name__ == "__main__":
    main()
