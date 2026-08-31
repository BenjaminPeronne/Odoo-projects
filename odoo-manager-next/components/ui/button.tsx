import { Button as RadixButton } from "@radix-ui/themes";
import * as React from "react";
import { cn } from "@/lib/utils";

type ButtonVariant = "default" | "secondary" | "outline" | "ghost" | "destructive";
type ButtonSize = "default" | "sm" | "icon";

const buttonVariants = ({ variant = "default", size = "default", className }: { variant?: ButtonVariant | null; size?: ButtonSize | null; className?: string } = {}) =>
  cn(
    "min-w-0 gap-2 text-center leading-snug [&_svg]:shrink-0",
    variant === "destructive" && "app-destructive-button",
    size === "icon" && "h-9 w-9 p-0",
    className,
  );

export interface ButtonProps
  extends Omit<React.ComponentPropsWithoutRef<typeof RadixButton>, "color" | "size" | "variant"> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

const Button = React.forwardRef<React.ElementRef<typeof RadixButton>, ButtonProps>(
  ({ className, variant = "default", size = "default", ...props }, ref) => {
    const radixVariant =
      variant === "secondary" ? "soft" : variant === "outline" ? "outline" : variant === "ghost" ? "ghost" : "solid";
    const color = variant === "destructive" ? "red" : variant === "secondary" || variant === "outline" || variant === "ghost" ? "gray" : "blue";
    const radixSize = size === "sm" || size === "icon" ? "2" : "3";

    return (
      <RadixButton
        ref={ref}
        variant={radixVariant}
        color={color}
        size={radixSize}
        className={buttonVariants({ variant, size, className })}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";

export { Button, buttonVariants };
