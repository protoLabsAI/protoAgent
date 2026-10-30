import { careerCoachFlow } from "./careercoach";
import { defineFlow } from "./runner";

defineFlow(careerCoachFlow(!!process.env.FLOW_BASE_URL));
