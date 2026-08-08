import frappeUIPreset from "frappe-ui/tailwind";

export default {
  presets: [frappeUIPreset],
  content: [
    "./index.html",
    "./src/**/*.{vue,ts}",
    "./node_modules/frappe-ui/src/components/**/*.{vue,ts}",
  ],
};
