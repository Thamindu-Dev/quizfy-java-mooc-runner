import org.junit.runner.Description;
import org.junit.runner.JUnitCore;
import org.junit.runner.Result;
import org.junit.runner.notification.Failure;
import org.junit.runner.notification.RunListener;

import java.io.FileWriter;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class JsonTestRunner {

    static class TestCaseResult {
        String name;
        String className;
        boolean passed = true;
        String message = "";
    }

    public static void main(String[] args) throws Exception {
        if (args.length < 2) {
            System.err.println("Usage: JsonTestRunner <outputJsonPath> <TestClass1> [TestClass2...]");
            System.exit(1);
        }

        String outputPath = args[0];
        Class<?>[] testClasses = new Class<?>[args.length - 1];
        for (int i = 1; i < args.length; i++) {
            testClasses[i - 1] = Class.forName(args[i]);
        }

        Map<String, TestCaseResult> results = new LinkedHashMap<>();

        JUnitCore core = new JUnitCore();
        core.addListener(new RunListener() {
            @Override
            public void testStarted(Description description) {
                TestCaseResult r = new TestCaseResult();
                r.name = description.getMethodName() != null
                        ? description.getMethodName()
                        : description.getDisplayName();
                r.className = description.getClassName() != null ? description.getClassName() : "";
                results.put(description.getDisplayName(), r);
            }

            @Override
            public void testFailure(Failure failure) {
                String key = failure.getDescription().getDisplayName();
                TestCaseResult r = results.get(key);
                if (r == null) {
                    r = new TestCaseResult();
                    r.name = failure.getDescription().getMethodName() != null
                            ? failure.getDescription().getMethodName()
                            : key;
                    r.className = failure.getDescription().getClassName() != null
                            ? failure.getDescription().getClassName()
                            : "";
                    results.put(key, r);
                }
                r.passed = false;
                String msg = failure.getMessage();
                if (msg == null || msg.trim().isEmpty()) {
                    msg = failure.getException() != null
                            ? failure.getException().toString()
                            : "Test failed";
                }
                r.message = msg;
            }
        });

        Result junitResult = core.run(testClasses);

        List<TestCaseResult> list = new ArrayList<>(results.values());
        int total = list.size();
        int failedCount = 0;
        for (TestCaseResult r : list) {
            if (!r.passed) failedCount++;
        }
        int passedCount = total - failedCount;
        boolean allPassed = junitResult.wasSuccessful() && total > 0 && failedCount == 0;

        StringBuilder json = new StringBuilder();
        json.append("{");
        json.append("\"passed\":").append(allPassed).append(",");
        json.append("\"total\":").append(total).append(",");
        json.append("\"passedCount\":").append(passedCount).append(",");
        json.append("\"failedCount\":").append(failedCount).append(",");
        json.append("\"tests\":[");

        for (int i = 0; i < list.size(); i++) {
            TestCaseResult r = list.get(i);
            if (i > 0) json.append(",");
            json.append("{");
            json.append("\"name\":\"").append(escape(r.name)).append("\",");
            json.append("\"className\":\"").append(escape(r.className)).append("\",");
            json.append("\"passed\":").append(r.passed).append(",");
            json.append("\"message\":\"").append(escape(r.message)).append("\"");
            json.append("}");
        }

        json.append("]}");

        try (FileWriter fw = new FileWriter(outputPath, StandardCharsets.UTF_8)) {
            fw.write(json.toString());
        }

        // Force exit in case student code spawned a non-daemon thread
        System.exit(0);
    }

    private static String escape(String s) {
        if (s == null) return "";
        StringBuilder sb = new StringBuilder(s.length() + 16);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':  sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\b': sb.append("\\b");  break;
                case '\f': sb.append("\\f");  break;
                case '\n': sb.append("\\n");  break;
                case '\r': sb.append("\\r");  break;
                case '\t': sb.append("\\t");  break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.toString();
    }
}
