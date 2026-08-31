import { Select as RadixSelect } from "@radix-ui/themes";
import * as React from "react";
import { cn } from "@/lib/utils";

export function Select(props: React.ComponentPropsWithoutRef<typeof RadixSelect.Root>) {
  return <RadixSelect.Root size="3" {...props} />;
}

export function SelectValue() {
  return null;
}

export const SelectTrigger = React.forwardRef<
  React.ElementRef<typeof RadixSelect.Trigger>,
  React.ComponentPropsWithoutRef<typeof RadixSelect.Trigger>
>(({ className, children: _children, ...props }, ref) => (
  <RadixSelect.Trigger ref={ref} variant="surface" className={cn("w-full", className)} {...props} />
));
SelectTrigger.displayName = "SelectTrigger";

export const SelectContent = React.forwardRef<
  React.ElementRef<typeof RadixSelect.Content>,
  React.ComponentPropsWithoutRef<typeof RadixSelect.Content>
>(({ className, ...props }, ref) => (
  <RadixSelect.Content ref={ref} position="popper" className={cn("z-50", className)} {...props} />
));
SelectContent.displayName = "SelectContent";

export const SelectItem = React.forwardRef<
  React.ElementRef<typeof RadixSelect.Item>,
  React.ComponentPropsWithoutRef<typeof RadixSelect.Item>
>(({ className, ...props }, ref) => <RadixSelect.Item ref={ref} className={className} {...props} />);
SelectItem.displayName = "SelectItem";
