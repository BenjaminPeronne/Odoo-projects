import { Checkbox as RadixCheckbox } from "@radix-ui/themes";
import * as React from "react";
import { cn } from "@/lib/utils";

export const Checkbox = React.forwardRef<
  React.ElementRef<typeof RadixCheckbox>,
  React.ComponentPropsWithoutRef<typeof RadixCheckbox>
>(({ className, ...props }, ref) => (
  <RadixCheckbox ref={ref} size="2" className={cn("shrink-0", className)} {...props} />
));
Checkbox.displayName = "Checkbox";
