import { Badge as RadixBadge } from "@radix-ui/themes";
import * as React from "react";
import { cn } from "@/lib/utils";

type BadgeVariant = "default" | "secondary" | "success" | "warning" | "destructive" | "outline";

export interface BadgeProps extends Omit<React.ComponentPropsWithoutRef<typeof RadixBadge>, "color" | "variant"> {
  variant?: BadgeVariant;
}

export function Badge({ className, variant = "secondary", size = "2", ...props }: BadgeProps) {
  const color =
    variant === "success" ? "green" : variant === "warning" ? "amber" : variant === "destructive" ? "red" : variant === "default" ? "orange" : "gray";
  const radixVariant = variant === "outline" ? "outline" : variant === "default" || variant === "destructive" ? "solid" : "soft";

  return (
    <RadixBadge
      className={cn("min-h-6 max-w-full items-center justify-center whitespace-nowrap px-2.5 text-xs font-semibold leading-none", className)}
      color={color}
      size={size}
      variant={radixVariant}
      {...props}
    />
  );
}
