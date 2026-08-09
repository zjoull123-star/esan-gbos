declare module "frappe-ui" {
  export const Button: import("vue").DefineComponent<{
    theme?: "gray" | "blue" | "green" | "red";
    variant?: "solid" | "subtle" | "outline" | "ghost";
    type?: "button" | "submit" | "reset";
    loading?: boolean;
    disabled?: boolean;
  }>;

  export const FormControl: import("vue").DefineComponent<{
    label?: string;
    description?: string;
    type?:
      | "date"
      | "datetime-local"
      | "email"
      | "month"
      | "number"
      | "password"
      | "search"
      | "tel"
      | "text"
      | "time"
      | "url"
      | "week"
      | "textarea";
    modelValue?: string | number;
    required?: boolean;
    size?: "sm" | "md";
    variant?: "subtle" | "outline";
  }>;
}
