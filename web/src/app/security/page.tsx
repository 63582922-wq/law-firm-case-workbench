import { redirect } from "next/navigation";

export default function SecurityPage() {
  // Technical identity/model controls are administrator-only deployment
  // concerns.  Keep them out of the lawyer's browser workflow rather than
  // rendering the legacy desktop settings surface at a public route.
  redirect("/");
}
