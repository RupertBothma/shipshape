import http from 'k6/http';
import exec from 'k6/execution';
import { check, sleep } from 'k6';
import { Counter, Rate } from 'k6/metrics';

const errors = new Rate('errors');
const ingressUnreachable = new Rate('ingress_unreachable');
const nonResponseErrors = new Counter('non_response_errors');

const baseUrl = __ENV.BASE_URL || 'https://127.0.0.1';
const hostHeader = __ENV.HOST_HEADER || 'prod.helloworld.shipshape.example.com';
const MAX_FAILURE_LOGS = 5;
let loggedFailures = 0;

export const options = {
  vus: Number(__ENV.VUS || 30),
  duration: __ENV.DURATION || '5m',
  thresholds: {
    http_req_failed: ['rate<0.02'],
    http_req_duration: ['p(95)<500', 'p(99)<900'],
    errors: ['rate<0.02'],
    ingress_unreachable: ['rate==0'],
    non_response_errors: ['count==0'],
  },
};

export default function () {
  const response = http.get(`${baseUrl}/`, {
    headers: {
      Host: hostHeader,
    },
  });

  if (!response || response.status === 0) {
    ingressUnreachable.add(1);
    nonResponseErrors.add(1);
    errors.add(1);

    const detail = response
      ? `status=${response.status} error=${response.error || 'unknown'} error_code=${response.error_code || 'unknown'}`
      : 'response object was undefined';

    exec.test.abort(`Ingress unreachable for BASE_URL=${baseUrl} Host=${hostHeader}. ${detail}`);
  }

  ingressUnreachable.add(0);

  const ok = check(response, {
    'status is 200': (r) => r.status === 200,
    'body is non-empty': (r) => !!r.body && r.body.length > 0,
  });

  if (!ok) {
    nonResponseErrors.add(1);
    if (loggedFailures < MAX_FAILURE_LOGS) {
      const preview = response.body ? String(response.body).slice(0, 120) : '<empty>';
      console.error(`[k6] unexpected response status=${response.status} body_preview=${preview}`);
      loggedFailures += 1;
    }
  }

  errors.add(!ok);
  sleep(0.2);
}
