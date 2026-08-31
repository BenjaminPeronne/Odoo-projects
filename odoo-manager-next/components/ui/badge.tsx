import { Badge as RadixBadge } from "@radix-ui/themes";
import * as React from "react";
import { cn } from "@/lib/utils";

type BadgeVariant = "default" | "secondary" | "success" | "warning" | "destructive" | "outline";

export interface BadgeProps extends Omit<React.ComponentPropsWithoutRef<typeof RadixBadge>, "color" | "variant"> {
  variant?: BadgeVariant;
}

export function Badge({ className, variant = "secondary", ...props }: BadgeProps) {
  const color =
    variant === "success" ? "green" : variant === "warning" ? "amber" : variant === "destructive" ? "red" : variant === "default" ? "blue" : "gray";
  const radixVariant = variant === "outline" ? "outline" : variant === "default" || variant === "destructive" ? "solid" : "soft";

  return <RadixBadge className={cn("font-semibold", className)} color={color} variant={radixVariant} {...props} />;
}
