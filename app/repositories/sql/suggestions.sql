-- Distances remain internal; cards expose only a rounded distance, never coordinates.
-- All filters and ordering run BEFORE LIMIT, including for older registered accounts.
WITH mine AS (
    SELECT lower(deporte_codigo) AS code, nivel
    FROM jsonb_to_recordset(%(sports)s::jsonb) AS s(deporte_codigo text, nivel int)
), candidates AS (
    SELECT u.id, u.nombre, u.apellido_paterno, u.fecha_nacimiento,
           u.foto_perfil, u.biografia, u.fecha_creacion,
           CASE WHEN %(latitude)s::float8 IS NULL OR p.latitud IS NULL THEN NULL ELSE
               12742 * asin(sqrt(least(1.0, greatest(0.0,
                   power(sin(radians(p.latitud::float8 - %(latitude)s::float8) / 2), 2)
                   + cos(radians(%(latitude)s::float8)) * cos(radians(p.latitud::float8))
                   * power(sin(radians(p.longitud::float8 - %(longitude)s::float8) / 2), 2)
               )))) END AS distancia_km,
           EXISTS (SELECT 1 FROM usuario_deporte d JOIN mine m ON m.code=lower(d.deporte_codigo)
                   WHERE d.usuario_id=u.id AND abs(d.nivel-m.nivel)<=1) AS nivel_coincidente,
           COALESCE((SELECT sum(1.0 - abs(d.nivel-m.nivel)/4.0)
                     FROM usuario_deporte d JOIN mine m ON m.code=lower(d.deporte_codigo)
                     WHERE d.usuario_id=u.id), 0)
           / greatest(1, (SELECT count(*) FROM mine)
                        + (SELECT count(*) FROM usuario_deporte d WHERE d.usuario_id=u.id)
                        - (SELECT count(*) FROM usuario_deporte d JOIN mine m ON m.code=lower(d.deporte_codigo)
                           WHERE d.usuario_id=u.id)) AS score
    FROM usuario u
    JOIN rol r ON r.id=u.rol_id
    LEFT JOIN email_verificacion ev ON ev.usuario_id=u.id
    LEFT JOIN preferencia_perfil p ON p.usuario_id=u.id
    LEFT JOIN preferencia_usuario pref ON pref.usuario_id=u.id
    WHERE u.is_active=TRUE AND u.id<>%(user_id)s AND r.nombre=ANY(%(roles)s)
      AND (ev.usuario_id IS NULL OR ev.verified_at IS NOT NULL)
      AND COALESCE(pref.disponibilidad_match, TRUE)
      AND (NOT %(filter_sport)s OR EXISTS (
          SELECT 1 FROM usuario_deporte d
          LEFT JOIN mine m ON m.code=lower(d.deporte_codigo)
          WHERE d.usuario_id=u.id
            AND (%(sport)s::text IS NULL OR lower(d.deporte_codigo)=lower(%(sport)s::text))
            AND d.nivel BETWEEN %(min_level)s AND %(max_level)s
            AND (NOT %(shared_sports)s OR m.code IS NOT NULL)
            AND (%(level_tolerance)s::int IS NULL OR abs(d.nivel-m.nivel)<=%(level_tolerance)s::int)
      ))
)
SELECT * FROM candidates
WHERE %(radius_km)s::float8 IS NULL OR distancia_km<=%(radius_km)s::float8
ORDER BY distancia_km ASC NULLS LAST, nivel_coincidente DESC, score DESC, fecha_creacion DESC, id
LIMIT %(limit)s
