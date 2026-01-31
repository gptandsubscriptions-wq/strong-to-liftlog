#!/usr/bin/env python3
"""
Strong to LiftLog Converter

Converts Strong app CSV exports to LiftLog backup format (.liftlogbackup.gz).
Based on the actual LiftLog protobuf schema from:
https://github.com/LiamMorrow/LiftLog/tree/main/proto

Usage:
    python strong_to_liftlog.py <strong_export.csv> [output.liftlogbackup.gz]
"""

import csv
import gzip
import math
import sys
import uuid
from collections import OrderedDict
from datetime import datetime

from google.protobuf.wrappers_pb2 import StringValue

from proto.ExportedDataDao.ExportedDataDaoV2_pb2 import ExportedDataDaoV2
from proto.ProgramBlueprintDao.ProgramBlueprintDaoV1_pb2 import (
    ProgramBlueprintDaoV1,
)
from proto.SessionBlueprintDao.SessionBlueprintDaoV2_pb2 import (
    ExerciseBlueprintDaoV2,
    RestDaoV2,
    SessionBlueprintDaoV2,
)
from proto.SessionHistoryDao.SessionHistoryDaoV2_pb2 import (
    PotentialSetDaoV2,
    RecordedExerciseDaoV2,
    RecordedSetDaoV2,
    SessionDaoV2,
)
from proto.Utils_pb2 import (
    DateOnlyDao,
    DecimalValue,
    TimeOnlyDao,
    UuidDao,
    ZoneOffsetDao,
)

# --- Constants ---
NANO_FACTOR = 1_000_000_000
EXERCISE_TYPE_WEIGHTED = 0
WEIGHT_UNIT_KG = 1


# --- UUID helpers ---

def uuid_to_guid_bytes(u: uuid.UUID) -> bytes:
    """Convert a UUID to C# Guid.ToByteArray() byte ordering.

    LiftLog was originally a .NET app, so UUIDs are stored in the format
    produced by C# Guid.ToByteArray(), which swaps the first three groups:
      Standard:  [0 1 2 3  4 5  6 7  8 9 10 11 12 13 14 15]
      C# GUID:   [3 2 1 0  5 4  7 6  8 9 10 11 12 13 14 15]
    """
    b = u.bytes
    return bytes([
        b[3], b[2], b[1], b[0],
        b[5], b[4],
        b[7], b[6],
        b[8], b[9], b[10], b[11], b[12], b[13], b[14], b[15],
    ])


def make_uuid_dao() -> UuidDao:
    """Create a UuidDao with a random UUID in C# GUID byte ordering."""
    dao = UuidDao()
    dao.value = uuid_to_guid_bytes(uuid.uuid4())
    return dao


# --- Decimal/weight helpers ---

def make_decimal_value(value: float) -> DecimalValue:
    """Convert a float to LiftLog's DecimalValue (units + nanos).

    DecimalValue uses int64 units + sfixed32 nanos (10^-9).
    For example, 72.5 -> units=72, nanos=500000000.
    """
    dv = DecimalValue()
    units = int(math.floor(value))
    nanos = int(round((value - units) * NANO_FACTOR))
    # Handle rounding pushing nanos to 1 billion
    if nanos >= NANO_FACTOR:
        units += 1
        nanos = 0
    dv.units = units
    dv.nanos = nanos
    return dv


# --- Date/time helpers ---

def make_date_dao(dt: datetime) -> DateOnlyDao:
    """Create a DateOnlyDao from a datetime."""
    dao = DateOnlyDao()
    dao.year = dt.year
    dao.month = dt.month
    dao.day = dt.day
    return dao


def make_time_dao(dt: datetime, offset_seconds: int = 0) -> TimeOnlyDao:
    """Create a TimeOnlyDao from a datetime, with an optional seconds offset.

    The offset_seconds is added to simulate time progression between sets
    (since Strong only records one timestamp per workout, not per set).
    """
    total_secs = dt.hour * 3600 + dt.minute * 60 + dt.second + offset_seconds
    # Clamp to valid time range (within 24 hours)
    total_secs = total_secs % 86400

    dao = TimeOnlyDao()
    dao.hour = total_secs // 3600
    dao.minute = (total_secs % 3600) // 60
    dao.second = total_secs % 60
    dao.millisecond = 0
    dao.microsecond = 0
    return dao


# --- CSV parsing ---

def parse_strong_csv(csv_path: str) -> OrderedDict:
    """Parse a Strong CSV export into grouped workout data.

    Returns an OrderedDict of workout_id -> {
        'datetime': datetime,
        'name': str,
        'duration_sec': int,
        'notes': str,
        'exercises': OrderedDict of exercise_name -> {
            'name': str,
            'sets': [{'weight': float, 'reps': int}]
        }
    }

    Skips warmup sets (Set Order = 'W') and rest timer rows
    (Set Order = 'Rest Timer').
    """
    workouts = OrderedDict()

    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter=";")

        for row in reader:
            workout_id = row["Workout #"]

            if workout_id not in workouts:
                dt = datetime.strptime(row["Date"], "%Y-%m-%d %H:%M:%S")
                duration = 0
                try:
                    duration = int(float(row.get("Duration (sec)", "0") or "0"))
                except ValueError:
                    pass

                workouts[workout_id] = {
                    "datetime": dt,
                    "name": row["Workout Name"] or "Workout",
                    "duration_sec": duration,
                    "notes": row.get("Workout Notes", "") or "",
                    "exercises": OrderedDict(),
                }

            set_order = row["Set Order"]
            exercise_name = row["Exercise Name"]
            workout = workouts[workout_id]

            # Capture notes from "Note" rows (Strong stores exercise notes
            # on separate rows with Set Order = "Note")
            if set_order == "Note":
                note = row.get("Notes", "") or ""
                if note and exercise_name in workout["exercises"]:
                    existing = workout["exercises"][exercise_name]["notes"]
                    if existing:
                        workout["exercises"][exercise_name]["notes"] = existing + "\n" + note
                    else:
                        workout["exercises"][exercise_name]["notes"] = note
                elif note:
                    # Note row appeared before any sets; store for later
                    workout.setdefault("_pending_notes", {})[exercise_name] = note
                continue

            # Skip warmup sets and rest timer rows
            if set_order == "W" or set_order == "Rest Timer":
                continue

            # Skip rows without a numeric set order
            try:
                int(set_order)
            except (ValueError, TypeError):
                continue

            if exercise_name not in workout["exercises"]:
                pending = workout.get("_pending_notes", {}).get(exercise_name, "")
                workout["exercises"][exercise_name] = {
                    "name": exercise_name,
                    "notes": pending,
                    "sets": [],
                }

            weight = 0.0
            try:
                weight = float(row["Weight (kg)"]) if row["Weight (kg)"] else 0.0
            except ValueError:
                pass

            reps = 0
            try:
                reps = int(float(row["Reps"])) if row["Reps"] else 0
            except ValueError:
                pass

            workout["exercises"][exercise_name]["sets"].append({
                "weight": weight,
                "reps": reps,
            })

    return workouts


# --- Protobuf building ---

def build_recorded_set(
    reps: int,
    workout_dt: datetime,
    set_index: int,
) -> RecordedSetDaoV2:
    """Build a RecordedSetDaoV2 for a completed set.

    Uses the workout datetime as a base and offsets each set by 60 seconds
    to simulate time progression between sets.
    """
    rs = RecordedSetDaoV2()
    rs.reps_completed = reps
    rs.completion_time.CopyFrom(make_time_dao(workout_dt, offset_seconds=set_index * 60))
    rs.completion_date.CopyFrom(make_date_dao(workout_dt))
    return rs


def build_potential_set(
    weight_kg: float,
    reps: int,
    workout_dt: datetime,
    set_index: int,
) -> PotentialSetDaoV2:
    """Build a PotentialSetDaoV2 with a recorded (completed) set inside."""
    ps = PotentialSetDaoV2()
    ps.weight_value.CopyFrom(make_decimal_value(weight_kg))
    ps.weight_unit = WEIGHT_UNIT_KG
    ps.recorded_set.CopyFrom(build_recorded_set(reps, workout_dt, set_index))
    return ps


def build_exercise_blueprint(name: str, num_sets: int, reps_per_set: int) -> ExerciseBlueprintDaoV2:
    """Build a minimal ExerciseBlueprintDaoV2 for a weighted exercise."""
    bp = ExerciseBlueprintDaoV2()
    bp.name = name
    bp.sets = num_sets
    bp.reps_per_set = reps_per_set
    bp.type = EXERCISE_TYPE_WEIGHTED
    return bp


def build_recorded_exercise(
    exercise_data: dict,
    workout_dt: datetime,
    set_offset: int,
) -> RecordedExerciseDaoV2:
    """Build a RecordedExerciseDaoV2 from parsed exercise data.

    set_offset is the cumulative set count from previous exercises in this
    workout, used to space out completion_time values across the session.
    """
    sets = exercise_data["sets"]
    name = exercise_data["name"]

    reps_first = sets[0]["reps"] if sets else 0
    blueprint = build_exercise_blueprint(name, len(sets), reps_first)

    rec_ex = RecordedExerciseDaoV2()
    rec_ex.exercise_blueprint.CopyFrom(blueprint)
    rec_ex.type = EXERCISE_TYPE_WEIGHTED

    if exercise_data.get("notes"):
        rec_ex.notes.CopyFrom(StringValue(value=exercise_data["notes"]))

    for i, set_data in enumerate(sets):
        ps = build_potential_set(
            weight_kg=set_data["weight"],
            reps=set_data["reps"],
            workout_dt=workout_dt,
            set_index=set_offset + i,
        )
        rec_ex.potential_sets.append(ps)

    return rec_ex


def build_session(workout_data: dict) -> SessionDaoV2:
    """Build a SessionDaoV2 from parsed workout data."""
    session = SessionDaoV2()
    session.id.CopyFrom(make_uuid_dao())
    session.session_name = workout_data["name"]
    session.date.CopyFrom(make_date_dao(workout_data["datetime"]))
    session.blueprint_notes = workout_data["notes"]

    cumulative_sets = 0
    for exercise_data in workout_data["exercises"].values():
        rec_ex = build_recorded_exercise(
            exercise_data,
            workout_data["datetime"],
            cumulative_sets,
        )
        session.recorded_exercises.append(rec_ex)
        cumulative_sets += len(exercise_data["sets"])

    return session


def build_backup(workouts: OrderedDict) -> ExportedDataDaoV2:
    """Build the complete ExportedDataDaoV2 backup from parsed workouts.

    Creates a minimal saved program so LiftLog has an active program context
    after import.
    """
    backup = ExportedDataDaoV2()

    for workout_data in workouts.values():
        if workout_data["exercises"]:
            session = build_session(workout_data)
            backup.sessions.append(session)

    # Create a saved program so LiftLog has something to set as active.
    # This uses a simple blueprint derived from the most recent workout.
    program_id = str(uuid.uuid4())
    program = ProgramBlueprintDaoV1()
    program.name = "Imported from Strong"

    # Build session blueprints from unique workout names
    seen_names = set()
    for workout_data in workouts.values():
        wname = workout_data["name"]
        if wname in seen_names:
            continue
        seen_names.add(wname)

        session_bp = SessionBlueprintDaoV2()
        session_bp.name = wname

        seen_exercises = set()
        for exercise_data in workout_data["exercises"].values():
            ename = exercise_data["name"]
            if ename in seen_exercises:
                continue
            seen_exercises.add(ename)

            sets = exercise_data["sets"]
            reps = sets[0]["reps"] if sets else 5

            ex_bp = ExerciseBlueprintDaoV2()
            ex_bp.name = ename
            ex_bp.sets = len(sets) if sets else 3
            ex_bp.reps_per_set = reps
            ex_bp.type = EXERCISE_TYPE_WEIGHTED
            ex_bp.weight_increase_on_success.CopyFrom(make_decimal_value(2.5))

            rest = RestDaoV2()
            rest.min_rest.seconds = 90
            rest.max_rest.seconds = 180
            rest.failure_rest.seconds = 300
            ex_bp.rest_between_sets.CopyFrom(rest)

            session_bp.exercise_blueprints.append(ex_bp)

        program.sessions.append(session_bp)

    today = datetime.now()
    program.last_edited.year = today.year
    program.last_edited.month = today.month
    program.last_edited.day = today.day

    backup.saved_programs[program_id].CopyFrom(program)
    backup.active_program_id.CopyFrom(StringValue(value=program_id))

    return backup


# --- Main ---

def main():
    if len(sys.argv) < 2:
        print("Usage: python strong_to_liftlog.py <strong_export.csv> [output.liftlogbackup.gz]")
        print()
        print("Converts a Strong app CSV export to a LiftLog backup file.")
        print("If no output file is specified, defaults to: liftlog_backup.liftlogbackup.gz")
        sys.exit(1)

    input_csv = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) >= 3 else "liftlog_backup.liftlogbackup.gz"

    print(f"Reading Strong export: {input_csv}")
    workouts = parse_strong_csv(input_csv)
    print(f"  Found {len(workouts)} workouts")

    total_exercises = sum(len(w["exercises"]) for w in workouts.values())
    total_sets = sum(
        len(s)
        for w in workouts.values()
        for e in w["exercises"].values()
        for s in [e["sets"]]
    )
    print(f"  Total exercise entries: {total_exercises}")
    print(f"  Total working sets: {total_sets}")

    print()
    print("Building LiftLog backup...")
    backup = build_backup(workouts)
    print(f"  Sessions in backup: {len(backup.sessions)}")
    print(f"  Saved programs: {len(backup.saved_programs)}")

    # Serialize and gzip
    proto_bytes = backup.SerializeToString()
    print(f"  Protobuf size: {len(proto_bytes)} bytes")

    with gzip.open(output_file, "wb") as f:
        f.write(proto_bytes)

    import os
    gz_size = os.path.getsize(output_file)
    print(f"  Gzipped size: {gz_size} bytes")

    print()
    print(f"Output saved to: {output_file}")
    print()
    print("To import in LiftLog:")
    print("  1. Copy the .liftlogbackup.gz file to your device")
    print("  2. Open LiftLog")
    print("  3. Go to Settings > Backup/Restore")
    print("  4. Tap 'Import from backup' and select the file")


if __name__ == "__main__":
    main()
