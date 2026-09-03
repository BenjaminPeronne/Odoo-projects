import { FileArchive, FolderOpen } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

type FilePickerProps = Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> & {
  buttonLabel?: string;
  emptyLabel?: string;
  file?: File | null;
};

export const FilePicker = React.forwardRef<HTMLInputElement, FilePickerProps>(
  (
    {
      accept,
      buttonLabel = "Choisir un fichier",
      className,
      disabled,
      emptyLabel = "Aucun fichier sélectionné",
      file,
      id,
      ...props
    },
    ref,
  ) => {
    const generatedId = React.useId();
    const inputId = id || generatedId;

    return (
      <div className={cn("min-w-0", className)}>
        <input
          ref={ref}
          id={inputId}
          type="file"
          accept={accept}
          disabled={disabled}
          className="sr-only"
          {...props}
        />
        <div
          className={cn(
            "flex min-h-14 min-w-0 items-center gap-3 rounded-md border border-input bg-card p-2 transition-[border-color,box-shadow] duration-150",
            "focus-within:border-primary focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2 focus-within:ring-offset-background",
            disabled && "bg-muted text-muted-foreground opacity-60",
          )}
        >
          <label
            htmlFor={inputId}
            aria-disabled={disabled || undefined}
            className={cn(
              "inline-flex min-h-10 shrink-0 cursor-pointer items-center justify-center gap-2 rounded-md bg-secondary px-3 text-sm font-semibold text-secondary-foreground transition-[background-color,box-shadow] duration-150 hover:bg-secondary/80",
              disabled && "pointer-events-none cursor-not-allowed bg-muted text-muted-foreground",
            )}
          >
            <FolderOpen className="h-4 w-4" aria-hidden="true" />
            {buttonLabel}
          </label>
          <span className="flex min-w-0 flex-1 items-center gap-2 text-sm">
            <FileArchive className="h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
            <span className={cn("min-w-0 truncate", !file && "text-muted-foreground")} title={file?.name}>
              {file?.name || emptyLabel}
            </span>
          </span>
        </div>
      </div>
    );
  },
);
FilePicker.displayName = "FilePicker";
