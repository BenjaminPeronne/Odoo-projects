import { TextField } from "@radix-ui/themes";
import * as React from "react";
import { cn } from "@/lib/utils";

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className, type, ...props }, ref) => {
    if (type === "file") {
      return (
        <input
          ref={ref}
          type="file"
          className={cn(
            "flex min-h-10 w-full rounded-md border border-input bg-card px-3 py-2 text-sm file:mr-3 file:border-0 file:bg-transparent file:text-sm file:font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50",
            className,
          )}
          {...props}
        />
      );
    }

    return (
      <TextField.Root
        ref={ref}
        type={type as React.ComponentPropsWithoutRef<typeof TextField.Root>["type"]}
        size="3"
        variant="surface"
        className={cn("w-full", className)}
        {...(props as React.ComponentPropsWithoutRef<typeof TextField.Root>)}
      />
    );
  },
);
Input.displayName = "Input";
