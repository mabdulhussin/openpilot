from cereal import car
from common.numpy_fast import clip, interp
from common.realtime import DT_MDL
from common.params import Params
from selfdrive.config import Conversions as CV
from selfdrive.modeld.constants import T_IDXS


# kph
V_CRUISE_MAX = 135
V_CRUISE_MIN = 1
V_CRUISE_DELTA = 5
V_CRUISE_OFFSET = 3
V_CRUISE_OFFSET_DEFAULT = 3
V_CRUISE_ENABLE_MIN = 5
LAT_MPC_N = 16
LON_MPC_N = 32
CONTROL_N = 17
CAR_ROTATION_RADIUS = 0.0

# this corresponds to 80deg/s and 20deg/s steering angle in a toyota corolla
MAX_CURVATURE_RATES = [0.03762194918267951, 0.003441203371932992]
MAX_CURVATURE_RATE_SPEEDS = [0, 35]

# Constants for Limit controllers.
LIMIT_ADAPT_ACC = -1.  # m/s^2 Ideal acceleration for the adapting (braking) phase when approaching speed limits.
LIMIT_MIN_ACC = -1.5  # m/s^2 Maximum deceleration allowed for limit controllers to provide.
LIMIT_MAX_ACC = 3.0   # m/s^2 Maximum acelration allowed for limit controllers to provide while active.
LIMIT_MIN_SPEED = 8.33  # m/s, Minimum speed limit to provide as solution on limit controllers.
LIMIT_SPEED_OFFSET_TH = -1.  # m/s Maximum offset between speed limit and current speed for adapting state.
LIMIT_MAX_MAP_DATA_AGE = 10.  # s Maximum time to hold to map data, then consider it invalid inside limits controllers.


class MPC_COST_LAT:
  PATH = 1.0
  HEADING = 1.0
  STEER_RATE = 1.0


class MPC_COST_LONG:
  TTC = 5.0
  DISTANCE = 0.1
  ACCELERATION = 10.0
  JERK = 20.0


def rate_limit(new_value, last_value, dw_step, up_step):
  return clip(new_value, last_value + dw_step, last_value + up_step)


def get_steer_max(CP, v_ego):
  return interp(v_ego, CP.steerMaxBP, CP.steerMaxV)

def set_v_cruise_offset(do_offset):
  global V_CRUISE_OFFSET
  if do_offset:
    V_CRUISE_OFFSET = V_CRUISE_OFFSET_DEFAULT
  else:
    V_CRUISE_OFFSET = 0

def update_v_cruise(v_cruise_kph, buttonEvents, enabled, cur_time, accel_pressed,decel_pressed,accel_pressed_last,decel_pressed_last, fastMode, fast_mode_enabled, vEgo_kph, v_cruise_last_changed, long_plan, speed_limit_last_deactivated, speed_limit_active):
  
  if cur_time - speed_limit_last_deactivated > 0.5:
    params = Params()
    if fast_mode_enabled:
      if enabled:
        if accel_pressed:
          if ((cur_time-accel_pressed_last) >= 0.6667 or (fastMode and (cur_time-accel_pressed_last) >= 0.333)):
            v_cruise_kph += V_CRUISE_DELTA - (v_cruise_kph % V_CRUISE_DELTA)
        elif decel_pressed:
          if ((cur_time-decel_pressed_last) >= 0.6667 or (fastMode and (cur_time-decel_pressed_last) >= 0.333)):
            v_cruise_kph -= V_CRUISE_DELTA - ((V_CRUISE_DELTA - v_cruise_kph) % V_CRUISE_DELTA)
        else:
          for b in buttonEvents:
            if not b.pressed:
              if b.type in [car.CarState.ButtonEvent.Type.accelCruise, car.CarState.ButtonEvent.Type.decelCruise] and speed_limit_active and long_plan in ['limit']:
                # deactivate move-fast speed limit control without changing set speed
                speed_limit_active = False
                return v_cruise_kph
              elif not long_plan in ['limit']:
                if b.type == car.CarState.ButtonEvent.Type.accelCruise:
                  if (not fastMode):
                    v_cruise_kph += 1
                elif b.type == car.CarState.ButtonEvent.Type.decelCruise:
                  if (not fastMode):
                    if (cur_time-v_cruise_last_changed >= 3) and vEgo_kph - v_cruise_kph > V_CRUISE_DELTA: # user pressed "set" after accelerating while engaged
                      v_cruise_kph = vEgo_kph
                    else:
                      v_cruise_kph -= 1
        v_cruise_kph = clip(v_cruise_kph, V_CRUISE_MIN, V_CRUISE_MAX)
    else:
      for b in buttonEvents:
        if enabled and not b.pressed:
          if b.type in [car.CarState.ButtonEvent.Type.accelCruise, car.CarState.ButtonEvent.Type.decelCruise] and speed_limit_active and long_plan in ['limit']:
            # deactivate move-fast speed limit control without changing set speed
            speed_limit_active = False
            return v_cruise_kph
          elif not long_plan in ['limit']:
            if b.type == car.CarState.ButtonEvent.Type.accelCruise:
              v_cruise_kph += V_CRUISE_DELTA - (v_cruise_kph % V_CRUISE_DELTA - V_CRUISE_OFFSET)
            elif b.type == car.CarState.ButtonEvent.Type.decelCruise:
              if (cur_time-v_cruise_last_changed >= 3) and vEgo_kph - v_cruise_kph > V_CRUISE_DELTA: # user pressed "set" after accelerating while engaged
                v_cruise_kph = vEgo_kph
              else:
                v_cruise_kph -= V_CRUISE_DELTA - ((V_CRUISE_DELTA - v_cruise_kph + V_CRUISE_OFFSET) % V_CRUISE_DELTA)
            v_cruise_kph = clip(v_cruise_kph, V_CRUISE_MIN, V_CRUISE_MAX)

  return v_cruise_kph


def initialize_v_cruise(v_ego, buttonEvents, v_cruise_last):
  for b in buttonEvents:
    # 250kph or above probably means we never had a set speed
    if b.type == car.CarState.ButtonEvent.Type.accelCruise and v_cruise_last < 250:
      return v_cruise_last

  return int(round(clip(v_ego * CV.MS_TO_KPH, V_CRUISE_ENABLE_MIN, V_CRUISE_MAX)))


def get_lag_adjusted_curvature(CP, v_ego, psis, curvatures, curvature_rates):
  if len(psis) != CONTROL_N:
    psis = [0.0 for i in range(CONTROL_N)]
    curvatures = [0.0 for i in range(CONTROL_N)]
    curvature_rates = [0.0 for i in range(CONTROL_N)]

  # TODO this needs more thought, use .2s extra for now to estimate other delays
  delay = CP.steerActuatorDelay + .2
  current_curvature = curvatures[0]
  psi = interp(delay, T_IDXS[:CONTROL_N], psis)
  desired_curvature_rate = curvature_rates[0]

  # MPC can plan to turn the wheel and turn back before t_delay. This means
  # in high delay cases some corrections never even get commanded. So just use
  # psi to calculate a simple linearization of desired curvature
  curvature_diff_from_psi = psi / (max(v_ego, 1e-1) * delay) - current_curvature
  desired_curvature = current_curvature + 2 * curvature_diff_from_psi

  max_curvature_rate = interp(v_ego, MAX_CURVATURE_RATE_SPEEDS, MAX_CURVATURE_RATES)
  safe_desired_curvature_rate = clip(desired_curvature_rate,
                                          -max_curvature_rate,
                                          max_curvature_rate)
  safe_desired_curvature = clip(desired_curvature,
                                     current_curvature - max_curvature_rate/DT_MDL,
                                     current_curvature + max_curvature_rate/DT_MDL)
  return safe_desired_curvature, safe_desired_curvature_rate
