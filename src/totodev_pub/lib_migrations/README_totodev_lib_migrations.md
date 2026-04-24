# Library Migrations Folder

This folder should contain migration instruction for when deep changes are made to the library that imply the need for the library users to inspect and modify their code.  Migration files should be placed in this folder so that users of the library may use them to understand changes and adapt code.  Only breaking or deep-impact changes should get a migration file.

## Migration Notes

Migration notes should be placed in date-stamped files whose name always begins with `mig_YYYY-MM-DD_*.*`.  Migration files can be any format but here are a few options:

- *.mdc : An agent/cursorules file for use with the Cursor IDE.  Provides instructions intended to be used by a coding assistant to deal with the change.
- *.md : A textual description of the change.  These documents are often intended to be read by a human.

Example:
    lib_migrations/mig_2025-11-03_renamed_class_xyz.mdc
    lib_migrations/mig_2025-10-15_query_master_refactoring.md


